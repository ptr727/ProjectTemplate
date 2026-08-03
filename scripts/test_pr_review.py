#!/usr/bin/env python3
"""Drive pr_review's digest and wait loop against responses they must read correctly.

The script exists to collapse a poll cycle into one invocation, so its failure mode is a wrong
answer rather than a crash: a review attributed to the wrong login, a review counted against a
stale head, or a wait that returns success while nothing landed. Each case below feeds a crafted
GraphQL payload and asserts the reading, with `gql` replaced so no case reaches the network.

Run as `python3 scripts/test_pr_review.py`, or under `python3 -m unittest discover -s scripts`.
"""
from __future__ import annotations
import contextlib, io, json, subprocess, sys, unittest
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
            merge: str = 'CLEAN', comments: list[dict] | None = None) -> dict:
    return {'headRefOid': HEAD, 'mergeable': 'MERGEABLE', 'mergeStateStatus': merge,
            'reviews': {'nodes': reviews}, 'reviewThreads': {'nodes': threads or []},
            'comments': {'nodes': comments or []}}


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
        self.assertEqual(LATE, answer['createdAt'])

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
