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


# The shape 28 of the 332 measured bodies carry: an overview, and no count of what was read.
# A body of no text at all is not one of the shapes, and the reader now says so, correctly.
OVERVIEW = '## Pull request overview\n\nThe change is narrow.\n'


def review(login: str = pr_review.REVIEWER, oid: str = HEAD, body: str = OVERVIEW,
           at: str = EARLY) -> dict:
    return {'author': {'login': login}, 'state': 'COMMENTED', 'commit': {'oid': oid},
            'body': body, 'submittedAt': at}


def comment(login: str = pr_review.REVIEWER, at: str = LATE,
            body: str = 'I have reached my quota limit and cannot review this now.') -> dict:
    return {'author': {'login': login}, 'createdAt': at, 'body': body}


COVERED = ('Copilot reviewed 3 out of 3 changed files in this pull request and generated '
           'no new comments.')


def collapsed(heading: str = 'Comments suppressed due to low confidence (1)',
              finding: str = 'a.py:12 The retry count is off by one.',
              covers: str = COVERED) -> str:
    """The section as its own `<details>` wrapper, under the round's own coverage line.

    The coverage line is the reviewer's, quoted from the corpus rather than invented: it sat here
    as filler that nothing asserted on, which is one of the two places the shape was already in
    this file while no case read it.
    """
    return (f'{OVERVIEW}\n{covers}\n\n<details>\n<summary>{heading}</summary>\n\n'
            f'{finding}\n\n</details>\n')


def nested(heading: str = '### Suppressed comments (2)',
           finding: str = '**a.py:12**\n* The retry count is off by one.',
           covers: str = '- **Files reviewed:** 1/1 changed files') -> str:
    """The section as a Markdown heading nested inside the `Review details` wrapper.

    The live shape as of 2026-08-05: the section is no longer its own `<details>` wrapper with a
    matching `<summary>`, it is a Markdown heading inside the wrapper that also carries the
    round's file and effort metadata, which trails the findings rather than preceding them.

    That metadata is where this shape states its coverage, and it is the second spelling of the
    line rather than a second wrapper. It sat here as filler that nothing asserted on too.
    """
    return ('### Ready to approve\n\nThe change is narrow.\n\n'
            '<details>\n<summary>File summaries</summary>\n\n'
            '| File | Description |\n\n</details>\n\n'
            f'<details>\n<summary>Review details</summary>\n\n{heading}\n\n{finding}\n\n'
            f'{covers}\n'
            '- **Review effort level:** Lite\n</details>\n')


REFUSED = ("Copilot wasn't able to review this pull request because it exceeds the maximum "
           'number of files (300). Try reducing the number of changed files and requesting a '
           'review from Copilot again.')


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
        self.answer(payload([review(body=OVERVIEW + '\nSuppressed comments (1)\n\n'
                                                     'a.py:12 Off by one.')]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=1', out)
        self.assertIn('a.py:12 Off by one.', out)

    def test_the_per_file_summary_block_beside_it_is_not_a_finding(self) -> None:
        """Every real body collapses a file table too, and reporting that is noise, not a finding."""
        body = (OVERVIEW + '\n<details>\n<summary>Show a summary per file</summary>\n\n'
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

    def test_a_heading_nested_inside_the_review_details_wrapper_reports(self) -> None:
        """The shape that reported `suppressed=0` over a body carrying two findings.

        The reviewer moved the section inside the `Review details` wrapper as a Markdown heading,
        so the wrapper's summary reads `Review details` and matches nothing, and the fallback that
        exists for a moved wrapper scans the body with every wrapper deleted, which deletes the
        region the heading now sits in. The two misses compound into a clean round over findings
        no thread will ever carry, which is the one failure this whole digest exists to prevent.
        """
        self.answer(payload([review(body=nested())]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=2', out)
        self.assertIn('The retry count is off by one.', out)

    def test_the_nested_count_is_the_heading_s_own_rather_than_the_wrapper_s(self) -> None:
        """The wrapper's summary carries no count, so reading it floors two findings to one."""
        self.answer(payload([review(body=nested(heading='### Suppressed comments (3)'))]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=3', out)
        # The block starts at its own heading, so the wrapper's summary is not the finding's header.
        self.assertNotIn('Review details', out)

    def test_both_the_wrapper_shape_and_the_nested_shape_report_in_one_run(self) -> None:
        """Both appear across the rounds of a single pull request, so neither replaces the other.

        Retargeting the parse from the old shape to the new one would report the same false clean
        one round later, on whichever shape the reviewer happened not to emit that time.
        """
        self.answer(payload([review(oid=OLD, body=collapsed(heading='Suppressed comments (2)')),
                             review(body=nested())]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=4', out)
        self.assertIn('(on_head=2 earlier=2)', out)

    def test_the_file_summary_wrapper_beside_a_nested_section_is_still_not_a_finding(self) -> None:
        """A body carries several wrappers, and scanning them all must not read the table as one."""
        self.answer(payload([review(body=nested())]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertNotIn('File summaries', out)
        self.assertNotIn('| File | Description |', out)

    def test_a_human_review_carrying_the_phrase_is_not_a_copilot_finding(self) -> None:
        self.answer(payload([review(login='ptr727', body=collapsed()), review()]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=0', out)


class TestRefusal(GqlCase):
    """The review that says it did not review, which carries the head and covers nothing.

    It is a formal review with the correct commit and no threads, so every check a clean pass
    satisfies it satisfies too, and the digest it renders is the clean pass byte for byte. The
    pull request it was observed on had 301 changed files, one over the reviewer's limit, and was
    one command from merging on a round that never ran.
    """

    def test_a_refusal_on_the_head_is_not_coverage(self) -> None:
        self.answer(payload([review(body=REFUSED)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('review_on_head=NO', out)
        self.assertIn('refusal=YES', out)
        # It happened, so it is still a round.
        # What it is not is a review of anything.
        self.assertIn('rounds=1', out)
        self.assertFalse(pr_review.reviewed_head(payload([review(body=REFUSED)])))

    def test_the_body_prints_whole_under_a_marker_naming_the_remedy(self) -> None:
        """Its wording is what separates a file-count refusal from a quota one, so it is not cut."""
        self.answer(payload([review(body=REFUSED)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('COPILOT REFUSED THIS ROUND', out)
        self.assertIn(REFUSED, out)

    def test_each_documented_phrasing_counts_including_the_typographic_apostrophe(self) -> None:
        """One phrasing alone is one rewording away from reporting a refusal as a review."""
        # The typographic apostrophe is an escape, since the charset rule governs this file too.
        # The case is about the byte the reviewer sends rather than the character on screen.
        for body in ("Copilot wasn't able to review this pull request because it is too large.",
                     'Copilot wasn\u2019t able to review this pull request.',
                     'Copilot was not able to review this pull request.',
                     'Copilot is unable to review this pull request right now.'):
            with self.subTest(body=body):
                self.assertEqual(body, pr_review.refusal_of({'body': body}))

    def test_a_clean_pass_and_an_ordinary_review_are_not_refusals(self) -> None:
        for body in ('Reviewed 3 of 3 changed files and generated no comments.',
                     collapsed(), nested(), ''):
            with self.subTest(body=body[:40]):
                self.assertEqual('', pr_review.refusal_of({'body': body}))

    def test_a_review_quoting_the_wording_below_its_overview_is_not_a_refusal(self) -> None:
        """This pull request's own review body is that quotation, and the shape has bitten once.

        The suppressed matcher read the whole body and reported the review that discussed
        suppressed findings as carrying them. The opening is the unit for that reason: a refusal
        is the whole body, so a match further down is a review describing the wording.

        This is also what fixes the unit at one line. The overview prose is the second line of
        every review body, and reading two lines reports this review as a refusal of itself.
        """
        body = ('## Pull request overview\n\nThis PR treats a review that says Copilot '
                "wasn't able to review a pull request as a terminal state rather than "
                'as coverage.\n\n- The digest now reports `unable to review` separately.\n')
        self.assertEqual('', pr_review.refusal_of({'body': body}))
        self.answer(payload([review(body=body)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('review_on_head=yes', out)
        self.assertIn('refusal=no', out)

    def test_a_refusal_from_an_earlier_round_is_spent(self) -> None:
        """A refusal is a statement about one commit, so the push that changed it retires it."""
        self.answer(payload([review(oid=OLD, body=REFUSED), review()]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('review_on_head=yes', out)
        self.assertIn('refusal=no', out)

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
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('review_on_head=yes', out)
        self.assertIn('refusal=no', out)
        self.assertNotIn('COPILOT REFUSED THIS ROUND', out)
        self.assertNotIn(REFUSED, out)

    def test_the_wait_returns_zero_where_coverage_followed_a_refusal_of_the_same_head(self) -> None:
        """The digest and the exit code read one payload, so neither may spend it differently."""
        self.answer(payload([review(body=REFUSED, at=EARLY), review(at=LATE)]))
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, pr_review.main(['wait', '7', '--repo', 'o/r']))
        self.assertNotIn('status=REVIEW_IS_A_REFUSAL', out.getvalue())

    def test_a_human_review_carrying_the_wording_is_not_the_reviewer_refusing(self) -> None:
        self.answer(payload([review(login='ptr727', body=REFUSED), review(oid=OLD)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('refusal=no', out)


class TestCoverage(GqlCase):
    """The round that covered the head and read part of the diff, which is a clean pass elsewhere.

    Measured over 332 Copilot review bodies on this repository: five rounds across three pull
    requests reported reading fewer files than the pull request changed, and all three merged.
    One of them changed three files, left one unread across both its rounds, and reported
    "generated no comments" each time.
    """

    def digest_for(self, *reviews: dict) -> str:
        self.answer(payload(list(reviews)))
        out, _ = pr_review.digest('o', 'r', 7)
        return out

    def test_each_vetted_spelling_of_a_full_round_reads_as_full(self) -> None:
        """The census: four tails on the first spelling, and the `Review details` bullet."""
        for covers in (
            'Copilot reviewed 7 out of 7 changed files in this pull request and generated '
            'no new comments.',
            'Copilot reviewed 7 out of 7 changed files in this pull request and generated '
            'no comments.',
            'Copilot reviewed 7 out of 7 changed files in this pull request and generated '
            '1 comment.',
            'Copilot reviewed 7 out of 7 changed files in this pull request and generated '
            '4 comments.',
            '- **Files reviewed:** 7/7 changed files',
        ):
            with self.subTest(covers=covers[:44]):
                self.assertEqual((pr_review.FULL, covers),
                                 pr_review.coverage_of({'body': covers}))

    def test_a_round_that_read_part_of_the_diff_is_a_failure_the_digest_names(self) -> None:
        """PR 592's shape: three changed files, one never read, and it merged."""
        line = ('Copilot reviewed 2 out of 3 changed files in this pull request and generated '
                'no comments.')
        out = self.digest_for(review(body=OVERVIEW + '\n' + line))
        self.assertIn('coverage=PARTIAL', out)
        self.assertIn('COVERAGE IS PARTIAL', out)
        # The counts print, since they are what say how much went unread and no thread carries it.
        self.assertIn(line, out)
        # The round did cover the head.
        # That reading was correct, and it was not the whole reading.
        self.assertIn('review_on_head=yes', out)

    def test_the_second_spelling_reports_a_partial_round_too(self) -> None:
        """Both spellings appear in the corpus to this day, so neither replaces the other."""
        out = self.digest_for(review(body=nested(covers='- **Files reviewed:** 2/3 changed files')))
        self.assertIn('coverage=PARTIAL', out)

    def test_the_bullet_spelling_is_a_coverage_line_on_its_label_alone(self) -> None:
        """Its label is the marker, so dropping the words after the counts is not dropping it.

        Requiring them read a bullet that had lost them as no statement at all, which is a
        silent `unstated` over a round that stated its coverage plainly.
        """
        self.assertEqual(['- **Files reviewed:** 4/4'],
                         pr_review.coverage_statements('- **Files reviewed:** 4/4'))
        self.assertEqual((4, 4), pr_review.read_coverage('- **Files reviewed:** 4/4 changed file'))

    def test_the_reviewer_s_name_alone_is_not_a_coverage_line(self) -> None:
        """The other opener keeps its text requirement, prose opening lines with that name too."""
        self.assertFalse(pr_review.is_coverage_line('Copilot answers a request with a comment.'))

    def test_a_singular_changed_file_reads_rather_than_blocks(self) -> None:
        """A one-file round means what it says, and stopping the fleet over an -s is crying wolf."""
        self.assertEqual((1, 1), pr_review.read_coverage(
            'Copilot reviewed 1 out of 1 changed file in this pull request and generated '
            'no comments.'))

    def test_counts_that_cannot_both_be_true_block_rather_than_read_as_full(self) -> None:
        """A round claiming it read more files than were changed is one this is parsing wrongly.

        Read as full coverage it fails open on the very statement saying something is off, which
        is the shape of every other failure here.
        """
        line = ('Copilot reviewed 8 out of 7 changed files in this pull request and generated '
                'no comments.')
        self.assertIsNone(pr_review.read_coverage(line))
        self.assertEqual(pr_review.UNVETTED, pr_review.coverage_of({'body': line})[0])
        self.assertIn(f'coverage line: {line}', pr_review.unrecognized_in(OVERVIEW + '\n' + line))

    def test_a_coverage_line_that_parses_to_nothing_names_this_script(self) -> None:
        """The wording has drifted once for each of the two patterns beside this one.

        Passing a shape it does not recognize is how a gate stops gating as the wording moves,
        so the unrecognized shape is reported and its remedy is stated as fixing this script.
        """
        line = 'Copilot reviewed most of the changed files in this pull request.'
        out = self.digest_for(review(body=OVERVIEW + '\n' + line))
        self.assertIn('coverage=UNVETTED', out)
        # It is one of the unrecognized shapes rather than a report of its own.
        # Both say the reader needs fixing, and one of them saying it once is the whole message.
        self.assertIn('UNRECOGNIZED REVIEWER OUTPUT (1)', out)
        self.assertIn(f'coverage line: {line}', out)

    def test_a_round_stating_no_coverage_at_all_is_unknown_rather_than_either_verdict(self) -> None:
        """28 of the 332 bodies are an overview and a change list, and that shape is current.

        It interleaves with the counted one throughout rather than preceding it, and one pull
        request carries both across its two rounds, so failing on it would cry wolf on about one
        review in twelve. Reporting it as coverage is the bug this reading exists to remove.
        """
        body = ('## Pull request overview\n\nThis PR updates the backlog.\n\n'
                '**Changes:**\n- Delete the shipped cluster from `TODO.md`.\n')
        out = self.digest_for(review(body=body))
        self.assertIn('coverage=unstated', out)
        self.assertNotIn('COVERAGE IS PARTIAL', out)
        self.assertIn('shapes=ok', out)

    def test_a_refusal_is_exempt_rather_than_an_unrecognized_shape(self) -> None:
        """It states no coverage by design, and `head_reviews` has already dropped it.

        Read as a round, every refusal becomes a spurious unvetted-shape failure sitting on top
        of the `refusal=YES` that already names the state and its remedy.
        """
        out = self.digest_for(review(body=REFUSED))
        self.assertIn('refusal=YES', out)
        self.assertIn('coverage=unstated', out)
        self.assertIn('shapes=ok', out)

    def test_coverage_is_read_from_the_head_rather_than_from_a_superseded_round(self) -> None:
        """A partial round describes one commit's diff, and the push that changes it is answered
        by a round reading the whole of the new one."""
        old = (OVERVIEW + '\nCopilot reviewed 2 out of 3 changed files in this pull request '
               'and generated no comments.')
        out = self.digest_for(review(oid=OLD, body=old),
                              review(body=OVERVIEW + '\n' + COVERED))
        self.assertIn('coverage=full', out)
        self.assertNotIn('COVERAGE IS PARTIAL', out)

    def test_the_worst_of_two_rounds_on_one_head_is_what_reports(self) -> None:
        """A head carries two rounds through a re-request, and both read the same diff."""
        partial = ('Copilot reviewed 2 out of 3 changed files in this pull request and '
                   'generated no comments.')
        self.assertEqual(pr_review.PARTIAL,
                         pr_review.head_coverage(payload([review(body=COVERED, at=EARLY),
                                                          review(body=partial, at=LATE)]))[0])


    def test_a_round_that_states_full_coverage_settles_it_over_one_stating_none(self) -> None:
        """Unknown is the absence of a statement rather than a bad one, so it loses to a count."""
        self.assertEqual(pr_review.FULL,
                         pr_review.head_coverage(payload([review(body='## Overview\n\nNarrow.'),
                                                          review(body=COVERED)]))[0])

    def test_prose_mentioning_changed_files_is_not_this_round_stating_its_coverage(self) -> None:
        """The false positive the suppressed matcher and the refusal matcher have each had once.

        A review of the pull request that adds this check discusses the wording it adds, and a
        body-wide match reads that discussion as the round's own count.
        """
        body = ('## Pull request overview\n\nThis PR reads the line saying Copilot reviewed 2 '
                'out of 3 changed files, so a partial round stops reporting as a clean pass.\n\n'
                '- The digest now carries a `coverage` field.\n')
        out = self.digest_for(review(body=body))
        self.assertIn('coverage=unstated', out)

    def test_a_quoted_line_in_a_fenced_block_is_not_this_round_stating_its_coverage(self) -> None:
        """131 of the 332 bodies carry a fence, and this change puts both spellings in the diff."""
        body = ('### Ready to approve\n\nThe vetted spellings read:\n\n```\n'
                'Copilot reviewed 2 out of 3 changed files in this pull request and generated '
                'no comments.\n- **Files reviewed:** 4/9 changed files\n```\n\n' + COVERED + '\n')
        body = '### Ready to approve\n\n' + body
        out = self.digest_for(review(body=body))
        self.assertIn('coverage=full', out)
        self.assertNotIn('COVERAGE IS PARTIAL', out)

    def test_a_human_review_carrying_a_coverage_line_is_not_the_reviewer_s_round(self) -> None:
        partial = ('Copilot reviewed 2 out of 3 changed files in this pull request and '
                   'generated no comments.')
        out = self.digest_for(review(login='ptr727', body=partial),
                              review(body=OVERVIEW + '\n' + COVERED))
        self.assertIn('coverage=full', out)


class TestUnrecognizedShapes(GqlCase):
    """A shape this script has no reader for blocks, rather than being read past.

    Every reader here keys on a structural marker, so a marker that changes spelling is a section
    the reader stops finding and reports as absent. All three failures on record have that shape:
    a suppressed heading reworded, a suppressed section moved inside another wrapper, and a
    coverage line nothing parsed. Each was caught after it had already reported a clean pass.

    The inventory is measured rather than imagined. Over the same 332 review bodies, with fenced
    blocks removed and text reduced to ASCII, the whole corpus is 7 headings, 6 summaries and 3
    metadata labels, and every body carries at least one of them.
    """

    def digest_for(self, *reviews: dict) -> str:
        self.answer(payload(list(reviews)))
        out, _ = pr_review.digest('o', 'r', 7)
        return out

    def test_every_vetted_marker_together_reads_as_recognized(self) -> None:
        """The corpus shape in one body, so the lists are held against what they were built from."""
        self.assertEqual([], pr_review.unrecognized_in(nested()))
        self.assertEqual([], pr_review.unrecognized_in(collapsed()))
        self.assertEqual([], pr_review.unrecognized_in(OVERVIEW))

    def test_a_heading_that_is_not_in_the_inventory_blocks(self) -> None:
        """A renamed section is one the reader stops finding, which it reports as nothing there."""
        out = self.digest_for(review(body=OVERVIEW + '\n### Confidence assessment\n\nHigh.\n'))
        self.assertIn('shapes=UNRECOGNIZED', out)
        self.assertIn('heading: ### Confidence assessment', out)

    def test_a_details_summary_that_is_not_in_the_inventory_blocks(self) -> None:
        """The suppressed section has already moved between wrappers once."""
        body = OVERVIEW + '\n<details>\n<summary>Withheld findings</summary>\n\nx\n</details>\n'
        out = self.digest_for(review(body=body))
        self.assertIn('summary: Withheld findings', out)

    def test_a_metadata_label_that_is_not_in_the_inventory_blocks(self) -> None:
        """The coverage line arrived as one of these bullets, so the next reading may too."""
        out = self.digest_for(review(body=nested() + '\n- **Confidence:** high\n'))
        self.assertIn('metadata label: Confidence', out)

    def test_a_body_carrying_no_heading_at_all_blocks(self) -> None:
        """Every measured body opens on a heading, so one with none is a format never seen.

        This is the arm that catches a rewrite wholesale rather than marker by marker, and it is
        also what catches the refusal wording drifting, since a refusal stops being exempt.
        """
        self.assertIn('body carrying no heading at all',
                      ' '.join(pr_review.unrecognized_in('Looks good to me.')))
        self.assertIn('body carrying no heading at all',
                      ' '.join(pr_review.unrecognized_in('')))

    def test_a_refusal_is_exempt_because_it_is_already_a_vetted_shape(self) -> None:
        """It is a bare paragraph by design, and `refusal=YES` already names its remedy."""
        self.assertEqual([], pr_review.unrecognized_in(REFUSED))
        out = self.digest_for(review(body=REFUSED))
        self.assertIn('shapes=ok', out)
        self.assertIn('refusal=YES', out)

    def test_a_refusal_whose_wording_drifted_stops_being_exempt_and_blocks(self) -> None:
        """The exemption is the pattern, so losing the pattern loses the exemption, not the check.

        This is the failure the refusal check was built for arriving one rewording later: the
        body would read as an ordinary review carrying the head and raising nothing.
        """
        drifted = 'Copilot has declined to review this pull request because it is too large.'
        self.assertEqual('', pr_review.refusal_of({'body': drifted}))
        out = self.digest_for(review(body=drifted))
        self.assertIn('shapes=UNRECOGNIZED', out)
        self.assertIn('refusal=no', out)

    def test_the_emoji_and_the_finding_count_are_normalized_rather_than_vetted(self) -> None:
        """Both change without the section changing, so comparing them raw blocks every review."""
        for heading in ('### \U0001F7E2 Ready to approve', '### \U0001F7E1 Changes recommended',
                        '### Suppressed comments (4)', '### Suppressed comments (11)'):
            with self.subTest(heading=heading):
                self.assertEqual([], pr_review.unrecognized_in(f'{heading}\n\nText.\n'))

    def test_a_marker_quoted_in_a_fenced_block_is_not_one_the_review_carries(self) -> None:
        """This change publishes the inventory, so a review of it quotes the lot back."""
        body = (OVERVIEW + '\nThe vetted headings are:\n\n```\n### Confidence assessment\n'
                '- **Confidence:** high\n```\n')
        self.assertEqual([], pr_review.unrecognized_in(body))

    def test_a_reviewer_login_that_is_not_the_one_every_query_filters_on_blocks(self) -> None:
        """A rename leaves every filter here matching nothing, which reads as no review at all.

        That is the quietest drift of the lot: the digest reports `rounds=0 review_on_head=NO`
        over a review that landed, and a wait polls out its whole timeout against it.
        """
        pr = payload([review(login='copilot-code-review-agent')])
        found = ' '.join(pr_review.unrecognized_shapes(pr))
        self.assertIn('reviewer login: copilot-code-review-agent', found)

    def test_the_coding_agent_and_a_human_are_not_the_reviewer_renamed(self) -> None:
        """`copilot-swe-agent` edits code and is not this reviewer under another name."""
        for login in ('copilot-swe-agent', 'ptr727', 'codecov[bot]', 'dependabot[bot]'):
            with self.subTest(login=login):
                self.assertEqual([], pr_review.unrecognized_shapes(payload([review(login=login)])))

    def test_the_block_names_the_hub_and_leaves_the_merge_to_the_maintainer(self) -> None:
        """The remedy is an issue where the reader lives, and the merge is not this script's call."""
        out = self.digest_for(review(body=OVERVIEW + '\n### Confidence assessment\n'))
        self.assertIn('UNRECOGNIZED REVIEWER OUTPUT (1)', out)
        self.assertIn(f'File an issue on {pr_review.HUB}', out)
        self.assertIn("maintainer's call rather than the agent's", out)

    def test_every_round_is_read_rather_than_the_head_s(self) -> None:
        """This asks whether the reader still understands the reviewer, which is not head-scoped.

        A shape that arrived one round ago is one every later round carries, so waiting for it to
        reach the head is waiting through the rounds it is already misreading.
        """
        out = self.digest_for(review(oid=OLD, body=OVERVIEW + '\n### Confidence assessment\n'),
                              review(body=OVERVIEW))
        self.assertIn('shapes=UNRECOGNIZED', out)
        self.assertIn(f'(round {OLD[:8]})', out)


class TestCoverageExitCodes(GqlCase):
    """`status` returned 0 unconditionally, so a partial round reported as a covered head."""

    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def partial(self) -> dict:
        return review(body=OVERVIEW + '\nCopilot reviewed 2 out of 3 changed files in this '
                                      'pull request and generated no comments.')

    def test_status_exits_forty_two_on_a_round_that_read_part_of_the_diff(self) -> None:
        self.answer(payload([self.partial()]))
        self.assertEqual(42, pr_review.main(['status', '7', '--repo', 'o/r']))
        self.assertIn('status=COVERAGE_IS_PARTIAL', self.out.getvalue())

    def test_status_exits_forty_three_on_a_wording_it_does_not_read(self) -> None:
        self.answer(payload([review(body=OVERVIEW +
                                     '\nCopilot reviewed some of the changed files.')]))
        self.assertEqual(43, pr_review.main(['status', '7', '--repo', 'o/r']))
        out = self.out.getvalue()
        self.assertIn('status=UNRECOGNIZED_REVIEWER_OUTPUT', out)
        self.assertIn('coverage=UNVETTED', out)

    def test_status_still_exits_zero_on_a_full_round_and_on_a_silent_one(self) -> None:
        """Failing the silent shape would fail roughly one review in twelve on this repository."""
        for body in (OVERVIEW + '\n' + COVERED, nested(), OVERVIEW):
            with self.subTest(body=body[:40]):
                self.answer(payload([review(body=body)]))
                self.assertEqual(0, pr_review.main(['status', '7', '--repo', 'o/r']))

    def test_wait_carries_the_same_codes_rather_than_ending_on_a_partial_round(self) -> None:
        """`reviewed_head` is what decided its zero, and coverage of the head is not coverage
        of the diff."""
        self.answer(payload([self.partial()]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(42, pr_review.main(['wait', '7', '--repo', 'o/r']))
        self.assertIn('coverage=PARTIAL', self.out.getvalue())

    def test_status_and_wait_both_exit_forty_three_on_an_unrecognized_shape(self) -> None:
        """Neither may report a clean pass over output the reader does not understand."""
        self.answer(payload([review(body=OVERVIEW + '\n### Confidence assessment\n')]))
        self.assertEqual(43, pr_review.main(['status', '7', '--repo', 'o/r']))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(43, pr_review.main(['wait', '7', '--repo', 'o/r']))
        self.assertIn('status=UNRECOGNIZED_REVIEWER_OUTPUT', self.out.getvalue())

    def test_an_unrecognized_shape_outranks_the_partial_coverage_code(self) -> None:
        """A reader that does not understand the output cannot be believed about the diff either."""
        body = (OVERVIEW + '\n### Confidence assessment\n\nCopilot reviewed 2 out of 3 changed '
                'files in this pull request and generated no comments.\n')
        self.answer(payload([review(body=body)]))
        self.assertEqual(43, pr_review.main(['status', '7', '--repo', 'o/r']))

    def test_a_refusal_still_exits_forty_one_rather_than_on_a_coverage_code(self) -> None:
        """The refusal names the round that declined, where a coverage code names a round."""
        self.answer(payload([review(body=REFUSED)]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(41, pr_review.main(['wait', '7', '--repo', 'o/r', '--timeout', '0']))


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

    def cli(self, argv: list[str]) -> int:
        """Every run names its repository, since the parser supplies no default for one."""
        return pr_review.main([*argv, '--repo', 'o/r'])

    def test_status_prints_the_digest_and_exits_zero(self) -> None:
        self.answer(payload([review()]))
        self.assertEqual(0, self.cli(['status', '7']))
        self.assertIn('pr=7', self.out.getvalue())

    def test_wait_returns_zero_once_the_review_lands_on_the_head(self) -> None:
        """The first poll already sees it, so the loop body never runs."""
        self.answer(payload([review()]))
        with mock.patch.object(pr_review.time, 'sleep') as slept:
            self.assertEqual(0, self.cli(['wait', '7']))
        slept.assert_not_called()
        self.assertIn('waited=', self.out.getvalue())

    def test_wait_polls_again_after_a_pending_round(self) -> None:
        """Each iteration re-reads the head, since a push during the wait moves it."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        with mock.patch.object(pr_review.time, 'sleep') as slept:
            self.assertEqual(0, self.cli(['wait', '7']))
        self.assertEqual(1, slept.call_count)

    def test_wait_exits_thirty_at_the_timeout_rather_than_reporting_success(self) -> None:
        """Pending is not failure and not success, so it takes a code of its own."""
        self.answer(payload([review(oid=OLD)]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(30, self.cli(['wait', '7', '--timeout', '0']))
        self.assertIn('status=PENDING', self.out.getvalue())

    def test_the_timeout_carries_the_digest_rather_than_a_bare_pending_line(self) -> None:
        """A wait that ends with no evidence reports a slow reviewer and a broken poll alike."""
        self.answer(payload([review(oid=OLD)], [thread('T1')]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(30, self.cli(['wait', '7', '--timeout', '0']))
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
            self.assertEqual(40, self.cli(['wait', '7', '--timeout', '0']))
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertIn('status=ANSWERED_OUTSIDE_REVIEW', out)
        self.assertIn('quota', out)

    def test_wait_exits_forty_one_on_a_review_that_says_it_did_not_review(self) -> None:
        """Returning zero here is the failure: the digest is the clean pass byte for byte."""
        self.answer(payload([review(body=REFUSED)]))
        with mock.patch.object(pr_review.time, 'sleep') as slept:
            self.assertEqual(41, self.cli(['wait', '7', '--timeout', '0']))
        # Terminal, so it ends the wait rather than polling out the timeout against it.
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertIn('status=REVIEW_IS_A_REFUSAL', out)
        self.assertIn('review_on_head=NO', out)
        self.assertIn(REFUSED, out)

    def test_the_liveness_reading_ends_the_wait_and_the_full_read_refuses_it(self) -> None:
        """The liveness query carries no bodies, so a refusal reads there as ordinary coverage.

        That is what ends the loop, and nothing decides on it: the full read that follows every
        wait carries the body and is where the exit code comes from.
        """
        bodyless = {k: v for k, v in review().items() if k != 'body'}
        self.answer(payload([bodyless]), payload([review(body=REFUSED)]))
        with mock.patch.object(pr_review.time, 'sleep') as slept:
            self.assertEqual(41, self.cli(['wait', '7', '--timeout', '600']))
        slept.assert_not_called()
        self.assertIn('status=REVIEW_IS_A_REFUSAL', self.out.getvalue())

    def test_a_landed_review_wins_over_an_older_answer(self) -> None:
        """Coverage is the success case, and a spent comment does not downgrade it to 40."""
        self.answer(payload([review(at=LATE)], comments=[comment(at=EARLY)]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, self.cli(['wait', '7']))

    def test_wait_stops_on_a_request_nothing_picked_up(self) -> None:
        """Waiting on cannot start a request nothing is acting on, so the wait says so and ends.

        The zero timeout is what this fails on rather than hangs on, and it also pins the order:
        the pickup is read before the clock, so the stall reports as itself instead of as PENDING.
        """
        self.answer(payload([review(oid=OLD)], pending=True))
        with mock.patch.object(pr_review, 'timeline',
                               return_value=[('review_requested', LATE)]), \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(50, self.cli(
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
            self.assertEqual(0, self.cli(
                ['wait', '7', '--pickup-grace', '0', '--timeout', '600']))

    def test_the_pickup_read_waits_out_the_grace_rather_than_running_per_poll(self) -> None:
        """It costs a second call, and inside the grace a pending request is just work in flight."""
        self.answer(payload([review(oid=OLD)], pending=True), payload([review()], pending=True))
        with mock.patch.object(pr_review, 'timeline', return_value=[]) as seen, \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, self.cli(
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
            self.assertEqual(30, self.cli(
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
            self.assertEqual(0, self.cli(
                ['wait', '7', '--pickup-grace', '0', '--timeout', '0']))
        out = self.out.getvalue()
        self.assertIn('review_on_head=yes', out)
        self.assertNotIn('status=REQUEST_NOT_PICKED_UP', out)

    def test_a_review_landing_during_the_last_read_wins_over_the_timeout(self) -> None:
        """Same disagreement at the other exit: printing coverage and returning PENDING."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, self.cli(['wait', '7', '--timeout', '0']))
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
            self.assertEqual(30, self.cli(
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
            self.assertEqual(40, self.cli(
                ['wait', '7', '--pickup-grace', '0', '--timeout', '0']))

    def test_the_repo_argument_splits_into_owner_and_name(self) -> None:
        self.answer(payload([review()]))
        with mock.patch.object(pr_review, 'digest', return_value=('x', 0)) as dig:
            self.assertEqual(0, pr_review.main(['status', '7', '--repo', 'owner/name']))
        self.assertEqual(('owner', 'name', 7), dig.call_args.args)

    def test_a_run_naming_no_repo_is_rejected_rather_than_sent_somewhere(self) -> None:
        """A default would send it to one repository, and every number resolves there.

        That is the failure this had twice: the digest rendered, nothing in it disagreed, and
        the run was reading a pull request in a repository nobody had named.
        """
        self.answer(payload([review()]))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                pr_review.main(['status', '7'])
        self.assertIn('--repo', err.getvalue())

    def test_a_repo_that_is_not_owner_slash_name_is_rejected_by_name(self) -> None:
        """The near-miss a required argument still admits, and unpacking it is a bare traceback."""
        for bad in ('ProjectTemplate', 'ptr727/', '/ProjectTemplate', 'a/b/c', ''):
            with self.subTest(repo=bad):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit):
                        pr_review.main(['status', '7', '--repo', bad])
                self.assertIn('OWNER/NAME', err.getvalue())

    def test_the_digest_names_the_repository_it_read(self) -> None:
        """A digest of the wrong pull request is well-formed, so the line has to say which one."""
        self.answer(payload([review()]))
        self.assertEqual(0, self.cli(['status', '7']))
        self.assertIn('repo=o/r pr=7', self.out.getvalue())


def rthread(tid: str, body: str = 'The retry count is off by one.', path: str = 'a.py',
            line: int = 12, resolved: bool = False,
            login: str = pr_review.REVIEWER) -> dict:
    """A thread as the reply query reads it, with `path` and `line` on the thread itself."""
    return {'id': tid, 'isResolved': resolved, 'path': path, 'line': line,
            'comments': {'nodes': [{'author': {'login': login}, 'body': body}]}}


def page(threads: list[dict], more: bool = False, cursor: str | None = None) -> dict:
    return {'nodes': threads, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}


LANDED = {'id': 'c1', 'url': 'https://github.com/o/r/pull/7#discussion_r1', 'body': 'Fixed in abc.'}


class ReplyCase(unittest.TestCase):
    """Base driving `reply` against crafted responses, so no case reaches the network."""

    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(mock.patch.object(pr_review, 'origin_owner', return_value='o'))
        self.docs: list[str] = []

    def wire(self, *pages: dict, reply: dict | None = LANDED, resolved: bool = True) -> None:
        """Answer the thread reads from `pages`, and each mutation from the given shape."""
        queue = list(pages) or [page([])]

        def fake(query: str, **variables: object) -> dict:
            self.docs.append(query)
            if 'reviewThreads' in query:
                return {'repository': {'pullRequest': {'reviewThreads': queue.pop(0)}}}
            if 'addPullRequestReviewThreadReply' in query:
                return {'addPullRequestReviewThreadReply': {'comment': reply}}
            if 'resolveReviewThread' in query:
                return {'resolveReviewThread': {'thread': {'isResolved': resolved}}}
            raise AssertionError(f'unexpected document: {query[:60]}')

        self.enterContext(mock.patch.object(pr_review, 'gh_graphql', side_effect=fake))

    def run_reply(self, *extra: str) -> int:
        return pr_review.main(['reply', '7', '--repo', 'o/r', '--match', 'retry count',
                               '--body', 'Fixed in abc.', *extra])

    def wrote(self) -> bool:
        return any('mutation' in d for d in self.docs)

    def resolved_a_thread(self) -> bool:
        return any('resolveReviewThread' in d for d in self.docs)


class TestReplySelectsWithoutAnId(ReplyCase):
    def test_the_matching_thread_is_answered_and_resolved(self) -> None:
        self.wire(page([rthread('t1')]))
        self.assertEqual(0, self.run_reply('--resolve'))
        self.assertIn('REPLIED_AND_RESOLVED', self.out.getvalue())
        self.assertIn(LANDED['url'], self.out.getvalue())

    def test_the_id_comes_from_the_query_rather_than_the_caller(self) -> None:
        """The whole point: the id each mutation carries is one this same run just read."""
        ids = []

        def capture(query: str, **variables: object) -> dict:
            if 'reviewThreads' in query:
                return {'repository': {'pullRequest':
                                       {'reviewThreads': page([rthread('t-from-the-query')])}}}
            ids.append(variables.get('threadId'))
            if 'addPullRequestReviewThreadReply' in query:
                return {'addPullRequestReviewThreadReply': {'comment': LANDED}}
            return {'resolveReviewThread': {'thread': {'isResolved': True}}}

        with mock.patch.object(pr_review, 'gh_graphql', side_effect=capture):
            self.assertEqual(0, self.run_reply('--resolve'))
        self.assertEqual(['t-from-the-query', 't-from-the-query'], ids)

    def test_no_match_writes_nothing_and_lists_what_is_open(self) -> None:
        """A no-match reads the same as an already-answered thread, so it stops rather than guesses."""
        self.wire(page([rthread('t1', body='An unrelated finding about naming.')]))
        self.assertEqual(60, self.run_reply('--resolve'))
        self.assertFalse(self.wrote())
        self.assertIn('NO_MATCH', self.out.getvalue())
        # The open threads print, or the reader's next move is to go hunting for an id.
        self.assertIn('unrelated finding', self.out.getvalue())

    def test_two_matches_refuse_rather_than_take_the_first(self) -> None:
        """`head -n 1` on an ambiguous match is how a reply lands on the wrong finding."""
        self.wire(page([rthread('t1', path='a.py'), rthread('t2', path='b.py')]))
        self.assertEqual(61, self.run_reply('--resolve'))
        self.assertFalse(self.wrote())
        self.assertIn('AMBIGUOUS', self.out.getvalue())
        for path in ('a.py', 'b.py'):
            self.assertIn(path, self.out.getvalue())

    def test_path_narrows_an_otherwise_ambiguous_match(self) -> None:
        self.wire(page([rthread('t1', path='a.py'), rthread('t2', path='b.py')]))
        self.assertEqual(0, self.run_reply('--resolve', '--path', 'b.py'))
        self.assertIn('b.py:12', self.out.getvalue())

    def test_a_resolved_thread_is_not_a_candidate(self) -> None:
        """It is answered, and replying again reopens a conversation nobody is reading."""
        self.wire(page([rthread('t1', resolved=True)]))
        self.assertEqual(60, self.run_reply('--resolve'))
        self.assertFalse(self.wrote())

    def test_the_match_follows_the_cursor_to_the_last_page(self) -> None:
        """A first page read as the whole set reports no match on a thread further along."""
        self.wire(page([rthread('t1', body='Something else.')], more=True, cursor='c1'),
                  page([rthread('t2')]))
        self.assertEqual(0, self.run_reply('--resolve'))
        self.assertIn('REPLIED_AND_RESOLVED', self.out.getvalue())

    def test_the_match_reads_the_finding_text_rather_than_a_line_number(self) -> None:
        """A fix push moves the line, and every lookup keyed to one then misses."""
        self.wire(page([rthread('t1', line=999, body='The RETRY COUNT is off by one.')]))
        self.assertEqual(0, self.run_reply('--resolve'))
        self.assertIn('REPLIED_AND_RESOLVED', self.out.getvalue())


class TestReplyConfirmsBeforeResolving(ReplyCase):
    def test_a_reply_returning_no_url_leaves_the_thread_open(self) -> None:
        """Three replies posted empty and the resolves still succeeded, closing them unanswered."""
        self.wire(page([rthread('t1')]), reply={'id': 'c1', 'url': None, 'body': ''})
        self.assertEqual(62, self.run_reply('--resolve'))
        self.assertFalse(self.resolved_a_thread())
        self.assertIn('REPLY_NOT_CONFIRMED', self.out.getvalue())

    def test_a_reply_whose_body_came_back_empty_leaves_the_thread_open(self) -> None:
        """A url alone says a comment exists, not that it carries the answer."""
        self.wire(page([rthread('t1')]), reply={'id': 'c1', 'url': LANDED['url'], 'body': '   '})
        self.assertEqual(62, self.run_reply('--resolve'))
        self.assertFalse(self.resolved_a_thread())

    def test_a_resolve_that_does_not_confirm_is_reported_rather_than_assumed(self) -> None:
        """The reply is already posted, so silence here leaves a thread open behind an answer."""
        self.wire(page([rthread('t1')]), resolved=False)
        self.assertEqual(63, self.run_reply('--resolve'))
        self.assertIn('RESOLVE_NOT_CONFIRMED', self.out.getvalue())

    def test_without_resolve_the_thread_is_answered_and_left_open(self) -> None:
        """A decline is resolved once its evidence is in the thread, which is the reader's call."""
        self.wire(page([rthread('t1')]))
        self.assertEqual(0, self.run_reply())
        self.assertFalse(self.resolved_a_thread())
        self.assertIn('status=REPLIED', self.out.getvalue())


class TestReplyStaysInScope(ReplyCase):
    def test_a_target_under_another_owner_is_refused_before_anything_is_read(self) -> None:
        """The incident shape: a write that lands on a stranger's repository."""
        self.wire(page([rthread('t1')]))
        code = pr_review.main(['reply', '7', '--repo', 'someone-else/r', '--match', 'retry',
                               '--body', 'Fixed.', '--resolve'])
        self.assertEqual(64, code)
        self.assertEqual([], self.docs)
        self.assertIn('OUT_OF_SCOPE', self.out.getvalue())

    def test_an_unreadable_origin_refuses_rather_than_assuming_scope(self) -> None:
        """An unverified scope is not a scope, and a check that cannot run reports itself."""
        self.wire(page([rthread('t1')]))
        with mock.patch.object(pr_review, 'origin_owner', return_value=None):
            self.assertEqual(64, self.run_reply('--resolve'))
        self.assertEqual([], self.docs)

    def test_a_sibling_repository_under_the_same_owner_is_in_scope(self) -> None:
        """The fleet is one owner, and that is the case the maintainer works in daily."""
        self.wire(page([rthread('t1')]))
        code = pr_review.main(['reply', '7', '--repo', 'o/some-other-repo', '--match',
                               'retry count', '--body', 'Fixed in abc.', '--resolve'])
        self.assertEqual(0, code)


class TestReplyArguments(unittest.TestCase):
    def err(self, argv: list[str]) -> str:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                pr_review.main(argv)
        return err.getvalue()

    def test_an_empty_body_is_rejected_rather_than_posted(self) -> None:
        """A thread resolved on an empty answer reads as addressed while carrying nothing."""
        for body in ('', '   '):
            with self.subTest(body=body):
                self.assertIn('--body', self.err(
                    ['reply', '7', '--repo', 'o/r', '--match', 'x', '--body', body]))

    def test_a_missing_match_is_rejected_rather_than_matching_everything(self) -> None:
        self.assertIn('--match', self.err(['reply', '7', '--repo', 'o/r', '--body', 'Fixed.']))

    def test_a_writing_option_on_a_reading_command_is_an_error(self) -> None:
        """Silently ignored, it reads as an option that took effect on a run that wrote nothing."""
        for flag in (['--body', 'Fixed.'], ['--match', 'x'], ['--resolve'], ['--path', 'a.py']):
            with self.subTest(flag=flag[0]):
                self.assertIn(flag[0], self.err(['status', '7', '--repo', 'o/r', *flag]))


PIN = 'actions/checkout@' + '9' * 40


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
        uses, shas = refs(f'Bumped to `uses: {PIN}` this round.')
        self.assertEqual([PIN], uses)
        self.assertEqual([], shas)

    def test_a_commit_a_verb_claims_this_branch_carries_is_read(self) -> None:
        for phrase in ('Fixed in `69688ec`', 'landed in 69688ec', 'Corrected by `69688ec`',
                       'shipped as `69688ec`'):
            with self.subTest(phrase=phrase):
                self.assertEqual(['69688ec'], refs(f'{phrase} on this branch.')[1])

    def test_a_commit_stated_as_history_is_not_a_claim_about_this_branch(self) -> None:
        """PR 592's shape: a `develop` commit named as history, correct and not on this head."""
        self.assertEqual([], refs('`9d85941` merged "Reaching the Hub" whole, so the cluster '
                                  'is deleted rather than annotated.')[1])

    def test_a_sha_inside_quoted_tool_output_is_not_a_claim(self) -> None:
        """PR 584's shape: a digest pasted to show what the tool prints."""
        self.assertEqual([], refs('pr=108 head=9f56a472 rounds=1 review_on_head=yes '
                                  'threads=0 merge=CLEAN')[1])

    def test_a_commit_in_another_repository_is_not_a_claim(self) -> None:
        """PR 571 and 568's shape, and neither carries a URL that would mark it as elsewhere."""
        for body in ('Read at Blog `main@2b132e4`. Verdict operational.',
                     'Both are on Blog\'s ground-truth `main` (`2b132e4`), verified by reading it.',
                     '`themes/README.md` records the upstream repository, commit `154d006e`.'):
            with self.subTest(body=body):
                self.assertEqual([], refs(body)[1])

    def test_an_all_hex_english_word_is_not_read_as_a_commit(self) -> None:
        """The digit backstop, so a verb this list gains later cannot start reading prose."""
        for word in ('accede', 'acceded', 'defaced', 'effaced'):
            with self.subTest(word=word):
                self.assertEqual([], refs(f'The clause was corrected as {word} above.')[1])

    def test_a_hex_run_shorter_than_gits_own_floor_is_not_a_commit(self) -> None:
        """Six is short enough to collide with an identifier, and git abbreviates to seven."""
        self.assertEqual([], refs('Fixed in `abc12` which is not a commit.')[1])

    def test_each_reference_is_reported_once_however_often_it_is_quoted(self) -> None:
        uses, shas = refs(f'Fixed in `69688ec`, again fixed in `69688ec`, '
                          f'and `uses: {PIN}` twice: `uses: {PIN}`.')
        self.assertEqual(['69688ec'], shas)
        self.assertEqual([PIN], uses)

    def test_a_description_naming_nothing_yields_nothing(self) -> None:
        self.assertEqual(([], []), refs('This widens a rule and adds a case for it.'))


class ClaimsCase(unittest.TestCase):
    """Base for the `claims` path, with the pull request and every read replaced."""

    def setUp(self) -> None:
        self.compare: dict[str, tuple[int, str, str]] = {}
        self.carried: set[str] | None = set()

    def run_claims(self, body: str, head: str = HEAD) -> tuple[int, str]:
        def rest(path: str, jq: str | None = None) -> subprocess.CompletedProcess:
            rc, out, err = self.compare.get(path, (1, '', 'gh: Not Found (HTTP 404)'))
            return subprocess.CompletedProcess([path], rc, out, err)

        with mock.patch.object(pr_review, 'gql',
                               return_value={'headRefOid': head, 'body': body}), \
                mock.patch.object(pr_review, 'gh_rest', side_effect=rest), \
                mock.patch.object(pr_review, 'head_carries', return_value=self.carried), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = pr_review.check_claims('ptr727', 'ProjectTemplate', 7)
        return code, out.getvalue()

    def answer(self, sha: str, status: str, head: str = HEAD) -> None:
        self.compare[f'repos/ptr727/ProjectTemplate/compare/{sha}...{head}'] = (0, status, '')

    def unread(self, sha: str, head: str = HEAD) -> None:
        self.compare[f'repos/ptr727/ProjectTemplate/compare/{sha}...{head}'] = (
            1, '', 'error connecting to api.github.com')


class TestClaimsReadsCommits(ClaimsCase):
    def test_a_commit_the_head_descends_from_is_clean(self) -> None:
        """The count is asserted beside the verdict, since `stale=0` over nothing read is also 0."""
        for status in ('ahead', 'identical'):
            with self.subTest(status=status):
                self.answer('69688ec', status)
                code, out = self.run_claims('Fixed in `69688ec`.')
                self.assertEqual(0, code)
                self.assertIn('commits=1 uses=0 stale=0 unread=0', out)

    def test_a_commit_the_repository_does_not_carry_is_stale(self) -> None:
        """The amended-away SHA: the body still names the commit the branch was rewritten off."""
        code, out = self.run_claims('Fixed in `deadbee1`.')
        self.assertEqual(70, code)
        self.assertIn('STALE COMMIT `deadbee1`', out)
        self.assertIn('carries no such commit', out)

    def test_a_commit_this_head_does_not_descend_from_is_stale_and_names_the_status(self) -> None:
        """A commit that exists and is not on this branch is the rebase case, not a missing one."""
        self.answer('dbd1cdc', 'diverged')
        code, out = self.run_claims('Landed in `dbd1cdc`.')
        self.assertEqual(70, code)
        self.assertIn('does not descend from it (diverged)', out)

    def test_a_commit_github_did_not_answer_for_is_undecided_rather_than_stale(self) -> None:
        """A network failure reported as a stale description sends a reader to fix correct prose."""
        self.unread('69688ec')
        self.answer('a6d7a4b', 'ahead')
        code, out = self.run_claims('Fixed in `69688ec`, then corrected in `a6d7a4b`.')
        self.assertEqual(0, code)
        self.assertIn('unread=1', out)
        self.assertIn('left undecided', out)

    def test_every_reference_undecided_reports_no_verdict_rather_than_a_clean_pass(self) -> None:
        """`stale=0` from a check that read nothing renders exactly like `stale=0` from one that did."""
        self.unread('69688ec')
        code, out = self.run_claims('Fixed in `69688ec`.')
        self.assertEqual(71, code)
        self.assertIn('NOTHING_WAS_READ', out)


class TestClaimsReadsUsesRefs(ClaimsCase):
    def test_a_ref_the_head_tree_carries_is_clean(self) -> None:
        self.carried = {PIN}
        code, out = self.run_claims(f'Pinned to `uses: {PIN}`.')
        self.assertEqual(0, code)
        self.assertIn('uses=1 stale=0', out)

    def test_a_ref_no_file_at_head_carries_is_stale(self) -> None:
        """The bump the branch reverted, with the description still quoting the pin it named."""
        self.carried = set()
        code, out = self.run_claims(f'Pinned to `uses: {PIN}`.')
        self.assertEqual(70, code)
        self.assertIn(f'STALE USES `{PIN}`', out)

    def test_an_unreadable_head_tree_is_undecided_rather_than_stale(self) -> None:
        self.carried = None
        code, out = self.run_claims(f'Pinned to `uses: {PIN}`.')
        self.assertEqual(71, code)
        self.assertIn('NOTHING_WAS_READ', out)

    def test_a_description_quoting_no_ref_reads_no_tree(self) -> None:
        """The archive is one request, and a body naming nothing has no reason to spend it."""
        with mock.patch.object(pr_review, 'gql',
                               return_value={'headRefOid': HEAD, 'body': 'Prose only.'}), \
                mock.patch.object(pr_review, 'head_carries') as tree, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, pr_review.check_claims('ptr727', 'ProjectTemplate', 7))
        tree.assert_not_called()

    def test_a_stale_ref_and_a_stale_commit_are_both_reported(self) -> None:
        """One failing reference does not end the read, since a body drifts in more than one place."""
        self.carried = set()
        code, out = self.run_claims(f'Fixed in `deadbee1`, pinned to `uses: {PIN}`.')
        self.assertEqual(70, code)
        self.assertIn('STALE COMMIT', out)
        self.assertIn('STALE USES', out)
        self.assertIn('stale=2', out)


class TestClaimsIsReadOnly(unittest.TestCase):
    def test_the_head_tree_read_asks_for_an_archive_and_nothing_else(self) -> None:
        source = (REPO / 'scripts' / 'pr_review.py').read_text(encoding='utf-8')
        self.assertIn('tarball/{head}', source)

    def test_an_unreadable_archive_reads_as_undecided_rather_than_as_an_empty_tree(self) -> None:
        """An empty tree carries no ref, so reading a failed download as one reports every ref stale."""
        for outcome in (subprocess.CompletedProcess([], 1, b'', b''),):
            with mock.patch.object(pr_review.subprocess, 'run', return_value=outcome):
                self.assertIsNone(pr_review.head_carries('ptr727', 'ProjectTemplate', HEAD, [PIN]))

    def test_gh_being_absent_leaves_the_tree_undecided_rather_than_aborting_claims(self) -> None:
        """This read cannot go through `gh_rest`, so it carries that helper's guards itself.

        Raising here would abort a run mid-way, where every other unreadable answer in this
        subcommand reports undecided and lets the caller see what was decided.
        """
        with mock.patch.object(pr_review.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertIsNone(pr_review.head_carries('ptr727', 'ProjectTemplate', HEAD, [PIN]))

    def test_a_hung_download_times_out_rather_than_hanging_the_run(self) -> None:
        with mock.patch.object(pr_review.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('gh', 1)):
            self.assertIsNone(pr_review.head_carries('ptr727', 'ProjectTemplate', HEAD, [PIN]))

    def test_the_archive_read_is_bounded_by_a_timeout(self) -> None:
        """An unbounded read of a whole repository is a wait with no end and no message."""
        with mock.patch.object(pr_review.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 1, b'', b'')
            pr_review.head_carries('ptr727', 'ProjectTemplate', HEAD, [PIN])
        self.assertEqual(pr_review.TARBALL_TIMEOUT, run.call_args.kwargs['timeout'])

    def test_an_archive_that_is_not_a_readable_tarball_is_undecided(self) -> None:
        proc = subprocess.CompletedProcess([], 0, b'not a gzip stream', b'')
        with mock.patch.object(pr_review.subprocess, 'run', return_value=proc):
            self.assertIsNone(pr_review.head_carries('ptr727', 'ProjectTemplate', HEAD, [PIN]))

    def test_a_ref_is_matched_as_bytes_so_an_undecodable_file_is_still_searched(self) -> None:
        """Skipping a file this cannot decode is how a present ref reads as absent."""
        source = (REPO / 'scripts' / 'pr_review.py').read_text(encoding='utf-8')
        self.assertIn('n in blob', source)

    def test_an_absent_status_is_absence_and_a_rate_limit_is_not(self) -> None:
        """Reading a 403 as a missing commit reports a correct description as contradicting itself."""
        for status, absent in (('404', True), ('422', True), ('403', False), ('401', False),
                               ('500', False)):
            with self.subTest(status=status):
                proc = subprocess.CompletedProcess([], 1, '', f'gh: (HTTP {status})')
                self.assertEqual(absent, pr_review.answered_absent(proc))

    def test_a_network_error_carrying_no_status_is_not_absence(self) -> None:
        proc = subprocess.CompletedProcess([], 1, '', 'error connecting to api.github.com')
        self.assertFalse(pr_review.answered_absent(proc))

    def test_gh_being_absent_does_not_raise(self) -> None:
        with mock.patch.object(pr_review.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertEqual(1, pr_review.gh_rest('repos/o/r').returncode)


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

    def test_the_refusal_pattern_is_the_runbook_alternation(self) -> None:
        """A refusal reworded once is a refusal read as coverage, so the pattern is not a memory."""
        text = RUNBOOK.read_text(encoding='utf-8')
        self.assertIn(f'test("{pr_review.REFUSAL.pattern}")', text)
        # The published filter is single-quoted, which no spelling of the apostrophe survives.
        self.assertNotIn("'", pr_review.REFUSAL.pattern)

    def test_the_vetted_coverage_spellings_are_the_ones_the_runbook_publishes(self) -> None:
        """The wording drifts, so the vetted list is read out of the runbook rather than recalled.

        The two published lines are pulled out with the script's own opener and handed to its own
        parser, which holds the pair in step in both directions: a spelling the runbook adds and
        this cannot read fails here, and so does one this reads that the runbook never named.
        """
        text = RUNBOOK.read_text(encoding='utf-8')
        published = [ln.strip() for ln in text.splitlines()
                     if pr_review.is_coverage_line(ln)]
        self.assertEqual(2, len(published), published)
        for line in published:
            with self.subTest(line=line[:44]):
                self.assertIsNotNone(pr_review.read_coverage(line))
        # The published filter is single-quoted in the shell, which no apostrophe survives.
        self.assertNotIn("'", pr_review.COVERAGE_COUNTS.pattern)

    def test_the_runbook_names_the_partial_round_as_a_state_that_blocks_a_merge(self) -> None:
        """A verify step reading `commit.oid` alone is what let five partial rounds merge."""
        text = RUNBOOK.read_text(encoding='utf-8')
        self.assertIn('Coverage of the head is not coverage of the diff', text)

    def test_the_only_writes_are_the_two_the_reply_path_owns(self) -> None:
        """One writing path, and everything else that changes state stays out of this script.

        The read subcommands are the bulk of it and a mutation reaching them is a digest that
        writes, so the whole-source guard stays and is narrowed to the two documents `reply`
        needs rather than dropped when the first of them arrived.
        """
        source = (REPO / 'scripts' / 'pr_review.py').read_text(encoding='utf-8')
        for verb in ('-X POST', '-X PATCH', '-X PUT', '-X DELETE', '--method',
                     'gh pr merge', 'gh pr review', 'gh pr edit', 'requestReviews'):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, source, f'{verb!r} is a state-changing call this script '
                                               'has no reason to make')
        # `mutation(` opens a document, so the count is the number of documents.
        # A third arriving is a write nobody reviewed as one rather than a style drift.
        self.assertEqual(2, source.count('mutation('))
        self.assertIn('addPullRequestReviewThreadReply', source)
        self.assertIn('resolveReviewThread', source)

    def test_the_mutations_are_the_ones_the_runbook_publishes(self) -> None:
        """A helper performing a different write than the documented one is undocumented."""
        text = RUNBOOK.read_text(encoding='utf-8')
        for name in ('addPullRequestReviewThreadReply', 'resolveReviewThread'):
            with self.subTest(mutation=name):
                self.assertIn(name, text)

    def test_no_argument_accepts_a_thread_id(self) -> None:
        """The failure is an id typed into a mutation, so the fix is having nowhere to type one.

        A parser that takes an id restores the failing shape however plainly the docs discourage
        it, which is the lesson the two prior instances taught: the rule was known and read.
        """
        source = (REPO / 'scripts' / 'pr_review.py').read_text(encoding='utf-8')
        # A node id literal anywhere in the source is an example a hand copies out of it.
        self.assertNotIn('PRRT_', source)
        parser_options = re.findall(r"add_argument\('(--[a-z-]+)'", source)
        for opt in parser_options:
            with self.subTest(option=opt):
                self.assertNotIn('id', opt.replace('--', '').split('-'))
        # The selector is the finding's text, and it is required rather than defaulted.
        self.assertIn("'--match'", source)

    def test_no_write_suppresses_or_forces_its_own_result(self) -> None:
        """A mutation whose output is discarded is a write nobody can say landed."""
        source = (REPO / 'scripts' / 'pr_review.py').read_text(encoding='utf-8')
        for tail in ('>/dev/null', '2>/dev/null', '&>/dev/null', '|| true', '|| :', 'shell=True'):
            with self.subTest(tail=tail):
                self.assertNotIn(tail, source)

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
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                pr_review.main(['wait', '7', '--repo', 'o/r', '--pickup-grace', '-1'])
        # The repository is named, or this exits on the missing argument and proves nothing.
        self.assertIn('pickup-grace', err.getvalue())

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
        self.assertGreaterEqual(loaded.countTestCases(), 48)


if __name__ == '__main__':
    unittest.main(verbosity=2)
