#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py>=4"]
# ///
"""Consolidated Copilot-review status for a PR - one command, one compact digest.

Why this exists: 4,513 of the measured `gh` invocations were one call per agent turn, and
each turn re-bills the whole session context. The bytes `gh` returns are trivial, averaging
574, so the round-trips are the entire cost. This collapses a poll cycle into a single
invocation whose output is a few hundred bytes. See GOVERNANCE.md "Context and Delegation
Discipline" for the rule this implements.

This file is the gate: the plumbing, the verdict, the wait loop, and the writes. The head's checks
are read by `pr_review_checks.py` and its review rounds by `pr_review_rounds.py`, both off a payload
this file fetched. What a reviewer's body says is read by `pr_review_copilot.py`, whose vocabulary lives in
`reviewers/<name>.json`, so a change in the reviewer's wording is a data edit there and never
an edit here. A part of a head round the adapter cannot place holds the gate. A trusted author's
written outcomes acknowledge it, and on an earlier round it is a note.

Subcommands
  attest   Publish that a recorded local pass covers the pull request's head, as a comment carrying
           `<!-- fleet-local-review: head=<sha> base=<branch> findings=<n> -->` that `status` and
           `wait` read, an attestation counting only against the base it names. It reads the
           receipt `local_review.py` keeps in the checkout's git directory and refuses unless the
           checkout (--checkout, default the current directory) is at the pull request's head with
           no change beyond it, its merge base with the base branch is the pull request's own, and
           `local_review.py status` reports a covering pass against the base. Exit 0 = posted,
           64 = the write scope could not be established or excludes the target, 65 = the pull
           request could not be read, 66 = the response did not confirm the comment, 67 = the
           checkout is not the head, holds changes, or measures another merge base, or that merge
           base could not be read, 68 = no current local pass covers the content.
  comment  Post one PR-conversation answer, including a suppressed-finding disposition. The PR
           node id is read in the same run, and the returned comment URL and body confirm the
           write. Exit 0 = done, 64 = write scope could not be established or excludes the
           target, 65 = the PR could not be read, 66 = the response did not confirm the comment.
           --fingerprint posts the body as the acknowledgment of the unplaced set `status` names,
           with `<!-- fleet-shape-ack: fingerprint=<fp> -->` beneath it.
  claims   Check the description against the branch it describes. A body claiming a commit or
           quoting a `uses:` ref the head tree no longer carries is a silent failure caught by a
           reviewer or not at all. Read-only. Exit 0 = every reference resolves, 70 = one does
           not, 71 = there were references and none could be read, so nothing was decided.
  cleanup  Forwarded to `pr_cleanup.py`, which holds the contract and the exit codes, so a command
           line naming this script keeps working under the allow rule the host-safety installer
           registers for it.
  status   One digest line, any unresolved threads, and any finding no thread carries. Read-only.
           Exit 0 = no Copilot review covers the head yet, or the review covering it states no
           partial coverage and its verdict counts what it flags. 42 = the round covering the
           head states it read fewer files than the pull request changed, or states nothing it
           can believe while an earlier round on record states partial coverage. 50 = that round
           opens on a verdict other than the clean one while it counts no finding, opens no thread,
           collapses no suppressed or previously-missed finding, and no thread is open, so what it
           flags is named in its headline alone, printed under `VERDICT WITH NO COUNTED FINDING`. The code
           repeats on that head after the headline is answered, since nothing here reads an
           answer. A clean verdict on the same terms prints its headline under `CLEAN VERDICT
           HEADLINE`, which changes no exit code. 50 is also a head round's part the adapter
           cannot place, `shapes=HELD` with an excerpt under `UNPLACED ON THE HEAD`. A trusted
           comment acknowledging its fingerprint clears it, `shapes=ACKNOWLEDGED`.
           `coverage=` names how the head's diff is shown read: `full` or `PARTIAL` as the round
           states it, `UNKNOWN` for a statement this cannot believe or a body the reader failed
           on (`PARTIAL` instead where an earlier round states partial coverage), `table` where a
           round covering the head names exactly the changed files in its own file table,
           `carried:table` where the newest round carrying a table does, `local` where an attested
           local pass covers a head on a pull request into a branch other than the default, and
           `none` otherwise. A table stands in only where no round on record states partial
           coverage and the whole review history is in view. `none` decides no exit code and
           prints a note, since Merge Gate item 2 in pr-review-conduct reads this field.
           A promotion re-reviews content its feature pull requests already carried, so the hosted
           review runs once per change, at the promotion. On a pull request into any other branch
           the attested local pass covers every head, `review_on_head=local` where no Copilot
           round covers it.
           `review_on_head` and `rounds=` name Copilot's own coverage, never "no review of any
           kind covers this head". `other_reviewed` names a tracked other reviewer that posted on
           the head, identity and commit only, and its empty body there is its own "reviewed,
           nothing to flag" shape, not a gap.
           `other_rate_limited` names one whose newest comment or review is its own rate-limit
           notice. `refusal=` reads the newest Copilot review where it is a refusal: `QUOTA` for
           the account quota or an error whose run log names a rate limit, `ERROR` for any other
           error refusal, `YES` for any other refusal. `status` exits 0 over a refusal, and `wait`
           gives it 41.
           `unresolved` counts every open thread whoever opened it, its breakdown attributing the
           known logins. Both `threads=` and `unresolved=` read one 100-thread page, a trailing
           `+` and a `THREADS TRUNCATED` block naming a cut page. `suppressed=`,
           `previously_missed=`, and `cr_outside_diff=N (on_head=X earlier=Y)` count findings
           collapsed into review bodies over every round, split into the head's and earlier ones,
           each printed whole since no thread carries it, a trailing `+` marking a cut reviews
           window. A tally holding a count the reader cannot believe reads `?`. `qodo_open=N`
           counts Qodo's findings comment entries with no Resolved or Dismissed badge, `unknown`
           where that comment can sit behind the window. `overview=T/M` is the open findings the
           round covering the head states beside the threads that round opened, `?` where it
           states no count. A count it cannot believe reads `?` too. `FINDINGS WITH NO THREAD`
           names findings the round names that no thread on the pull request carries. Each of these is owed the triage a suppressed
           finding gets, and none decides an exit code.
  reply    Answer one thread selected by its text, and resolve it on request. Exists
           because the hand-run form keeps failing the same way: a node id typed into a
           mutation, which resolves globally and so writes to a real thread somewhere
           rather than failing. This takes a pull request number and words from the
           finding, queries the id itself, and offers no argument an id fits in. A finding
           posted twice on one file, threads whose opening comments read the same, is
           answered as one under --all-identical. Exit 0 = done, 60 = no thread matched,
           61 = more than one did and the flag did not apply, 62 = the reply returned
           no comment url so nothing was resolved, 63 = the resolve did not report the
           thread resolved, 64 = the write scope could not be established or excludes the target.
  wait     Request a review where none is outstanding, then poll until Copilot's review lands on the
           current head, then print the digest. The auto-request is skipped once a review past
           --min-rounds covers the head, once Copilot has answered outside a formal review, once
           one is pending, and under a refusal or quota reading below, since a request into a
           reached limit spends quota and returns the same refusal. It is skipped on a pull
           request into a branch other than the default too, whose every head an attested local
           pass covers: an attested head has its checks polled instead while they can still move
           its merge, and one with no attestation exits 49. --request asks for a round anyway.
           The bot id is read from the repository's most recently updated pull requests, the last
           HISTORY_PRS, widened once to HISTORY_PRS_WIDE, and nothing is requested where neither
           carries any Copilot activity. The loop runs in-process, so a 45-minute wait costs one
           agent turn, not 90.
           Exit 0 = review present, or on a held head an attested local pass whose checks no longer
           hold the poll. On an open pull request whose head commit the payload carries, every
           workflow run the read reaches on that head has then completed. The read reaches the
           head's last 50 check suites and those of its first CHECKS_WINDOW rollup contexts. A
           CLEAN, UNSTABLE, or HAS_HOOKS merge owes no required check. Under any other merge, an
           open attested held head nothing holds exits 45 where its rollup was unreadable or none
           posted. A DIRTY or BEHIND one also exits 45 where a required check is unregistered or
           from another app. Exit 0 can then leave a required check unregistered only under unread
           rules, a `CHECKS TRUNCATED` rollup, a DRAFT merge, or a pull request not open.
           30 = still pending at the timeout. On a held head it is any hold
           `status=CHECKS_PENDING` names, a required check still settling and a workflow run still
           open among them. A BLOCKED hold's note names the rollup's unread part as a place an
           unmet required check may sit. It is also a round past --min-rounds not yet landed.
           40 = no Copilot review covers the head: the reviewer
           answered outside a formal review, the request returned success and recorded nothing on
           the pull request, or the reviewer writes under a login no query filters on. 41 = the
           newest round is a refusal or a quota state, the kind printed in its status line and, for
           a refusal on this pull request, in the digest's `refusal=`: a refusal on the head, the
           pull request's newest review being a quota or error refusal on any head, or the
           reviewer's newest activity elsewhere in the repository being a quota refusal nothing
           answered since while this head carries none of its own. Proceed on the other reviewers'
           coverage rather than retrying, and pass --ignore-quota-signal once the quota is believed
           to have reset. 42 and 50 = the review landed and `status`'s readings
           apply to it. 44 = the review loop closed, the merge reads BLOCKED, and a required check
           is in a shape no wait clears. It is queued with nothing acting on it, expected and
           never posted, running far past the job's cost, held by its environment, or failed. On
           a held head a check running long or held by its environment polls to 30 instead.
           45 = an open attested held head that nothing holds, its merge other than CLEAN,
           UNSTABLE, or HAS_HOOKS. Either the payload carries no commit matching the head, named
           `CHECKS UNREADABLE` in the digest, or no check has posted. Or the merge reads DIRTY or
           BEHIND and a check the base branch's rules require has not registered, or came from
           another app. That last needs the rules read and a rollup not `CHECKS TRUNCATED`. Where
           the rollup holds a type this cannot read, named `CHECKS PARTIALLY UNREAD` in the
           digest, the status says the check may sit there.
           `status=CHECKS_MISSING` names which. A DIRTY merge runs no pull_request workflow until
           its conflict is resolved. A required check not yet registered holds a BLOCKED merge to
           30 instead, where the rules were read and the rollup is not `CHECKS TRUNCATED`. An empty
           rollup read off the head holds a BLOCKED, BEHIND, or DRAFT one.
           49 = no Copilot round covers this head and none was requested, the pull request
           merging into a branch other than the default with no attestation vouching for the
           head. 64 = the write scope could not
           be established or excludes the target, checked before the auto-request or any poll.

Reading is the bulk of this and the writing commands are a trade rather than a free win. A
mutation spelled as a `gh` command in a shell is read by the gh-write-guard PreToolUse hook, and
one this script performs is not. The script removes the guarded failure at its source: every node
id comes from a live query in the same run, and the owner boundary is enforced in-process. See
.github/copilot-instructions.md for the runbook, and GOVERNANCE.md "Repository Boundaries and
Write Safety" for the rules these commands enforce.
"""

from __future__ import annotations

import argparse
import functools
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import gh_graphql as shared
import pr_review_copilot as adapter
from pr_review_checks import (
    CHECK_GRACE,
    CHECK_STALL,
    CHECKS_WINDOW,
    WORKFLOW_KEYS_UNAVAILABLE,
    _unmet_note,
    check_lines,
    check_nodes,
    checks_never_ran,
    checks_settling,
    checks_stuck,
    checks_tally,
    held_checks_open,
    runs_open,
)
from pr_review_rounds import (
    ACK_MARKER,
    CARRIED_PARTIAL,
    CODERABBIT_LOGIN,
    FILES_UNREAD,
    KNOWN_REVIEWERS,
    OTHER_REVIEWERS,
    QODO_LOGIN,
    REVIEWER,
    answered_outside_review,
    attested,
    collected,
    copilot_bot_id,
    coverage,
    describe,
    files_unread,
    finding_text,
    first_comment,
    head_review_done,
    head_reviews,
    history_entry,
    holds,
    local_cover,
    matching_threads,
    newest_first,
    newest_of,
    open_threads,
    opened_by,
    possible_quota,
    qodo_comment,
    qodo_comments_blind,
    quota_refusal,
    quota_signal,
    rate_limited_by,
    reading,
    refusing_review,
    request_recorded,
    review_effort,
    reviewed_head,
    reviewer_login_drift,
    reviewer_nodes,
    reviewer_requested,
    reviews_truncated,
    shape_hold,
    standing_quota,
    stopping_refusal,
    thread_author,
    threads_truncated,
    uncounted_verdict,
    window_blind,
)

# The directory this script sits in, the anchor every scope answer is read from.
HERE = Path(__file__).resolve().parent


COPILOT_RUN_PATH = "dynamic/agents/copilot-pull-request-reviewer"
RUN_ERROR_TYPE = re.compile(r"errorType: '([a-z_]+)'")
RUN_RATE_LIMIT = re.compile(
    r"(You.ve reached your [^\n]*?rate limit\.[^\n]*?)(?= or switch| Learn More|$)", re.MULTILINE
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
ISO_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


# The repository this script is hosted in, where an unrecognized shape is fixed, not where it was seen.
HUB = "ptr727/ProjectTemplate"

# How many of the newest reviews and comments both queries read, held equal to the queries by a test.
WINDOW = 100

FILES_WINDOW = 100
FILES_CAP = 3000
READ_RETRY_DELAYS = (2, 5)
ATTEST_HEAD_DELAYS = (3, 5, 10, 15, 15, 12)
_attest_sleep = time.sleep
# What a git ref may not hold before it is interpolated into a REST path.
# Refused rather than allow-listed, since git permits more than a branch name usually carries.
# The set refused is what changes the path: `#`, `?`, `%`, whitespace, and control characters.
UNSAFE_REF = re.compile(r"[\s#?%]|[\x00-\x1f\x7f]")
# A dot segment travels up the path and reaches another repository.
DOT_SEGMENT = re.compile(r"(?:^|/)\.\.(?:/|$)")


# How many pull requests, and how much reviewer history within each, the repo-wide history read covers.
# Smaller than WINDOW and without a `hasPreviousPage` guard, since this is a best-effort signal.
HISTORY_PRS = 20
HISTORY_REVIEWS = 20
HISTORY_COMMENTS = 5

# The one-time widened retry `copilot_history` makes where HISTORY_PRS carries no Copilot activity.
# GitHub's connection ceiling for a single `first`, so further reach would need pagination.
HISTORY_PRS_WIDE = 100

# Liveness query: timestamps and ids only, no bodies, plus the reviewer's non-review answers.
Q_LIVE = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    id headRefOid
    reviews(last:100){ nodes{ author{login} state commit{oid} submittedAt } pageInfo{ hasPreviousPage } }
    comments(last:100){ nodes{ author{login} createdAt } pageInfo{ hasPreviousPage } }
    reviewRequests(first:10){ nodes{ requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } }
  }}}
"""

# The reviewer's own review and comment history, read live from the repository's pull requests.
# Two callers share it: the bot id lookup, and the repo-wide recency reading that needs the timestamps.
# Ordered by `UPDATED_AT` so a re-reviewed older pull request stays inside the window.
# `$prs` is a variable so one document serves both widths, and no `states` argument means merged pull requests count.
Q_BOT_ID = """
query($o:String!,$r:String!,$prs:Int!,$reviews:Int!,$comments:Int!){
  repository(owner:$o,name:$r){
    pullRequests(first:$prs, orderBy:{field:UPDATED_AT, direction:DESC}){
      nodes{ number
        reviews(last:$reviews){ nodes{ author{ __typename login ... on Bot{ id } } state body submittedAt } }
        comments(last:$comments){ nodes{ author{ login } body createdAt } }
      } } } }
"""

REQUEST_STATE = """
    reviewRequests(first:100){ nodes{ requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } }
    timelineItems(last:100, itemTypes:[REVIEW_REQUESTED_EVENT]){ nodes{
      ... on ReviewRequestedEvent{ id requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } } }
"""
Q_REQUEST_STATE = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ id __REQUEST_STATE__ }}}
""".replace("__REQUEST_STATE__", REQUEST_STATE)
M_REQUEST_REVIEWS = """
mutation($pr:ID!,$bot:ID!){
  requestReviews(input:{pullRequestId:$pr, botIds:[$bot], union:true}){ pullRequest{ id __REQUEST_STATE__ } }}
""".replace("__REQUEST_STATE__", REQUEST_STATE)

# The rollup rides this query and hangs off the last commit, which a case holds equal to `headRefOid`.
HELD_FIELDS = """
    headRefOid baseRefName state mergeable mergeStateStatus
    baseRepository{ defaultBranchRef{ name } }
    reviews(last:100){ nodes{ id author{login} state commit{oid} submittedAt body } pageInfo{ hasPreviousPage } }
    comments(last:100){ nodes{ author{login} authorAssociation createdAt body isMinimized } pageInfo{ hasPreviousPage } }
    reviewRequests(first:10){ nodes{ requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } }
    commits(last:1){ nodes{ commit{ oid statusCheckRollup{ state
      contexts(first:__CHECKS_WINDOW__){ pageInfo{ hasNextPage } nodes{
        __typename
        ... on CheckRun{ name status conclusion startedAt isRequired(pullRequestNumber:$n)
          checkSuite{ databaseId status app{ slug databaseId } workflowRun{ databaseId workflow{ databaseId } } } }
        ... on StatusContext{ context state createdAt isRequired(pullRequestNumber:$n) }
      }}}
      checkSuites(last:50){ nodes{ databaseId status app{ slug } workflowRun{ databaseId workflow{ databaseId } } } }
    }}}
""".replace("__CHECKS_WINDOW__", str(CHECKS_WINDOW))
Q_HELD = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ __HELD_FIELDS__ }}}
""".replace("__HELD_FIELDS__", HELD_FIELDS)
Q_FULL = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ __HELD_FIELDS__
    reviewThreads(first:100){ nodes{ id isResolved
      comments(first:1){ nodes{ author{login} path line body fullDatabaseId pullRequestReview{ id } } } } pageInfo{ hasNextPage } }
    files(first:__FILES_WINDOW__){ pageInfo{ hasNextPage endCursor } nodes{ path } }
  }}}
""".replace("__HELD_FIELDS__", HELD_FIELDS).replace("__FILES_WINDOW__", str(FILES_WINDOW))
Q_FILES = """
query($o:String!,$r:String!,$n:Int!,$after:String){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ headRefOid
    files(first:__FILES_WINDOW__, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ path } }
  }}}
""".replace("__FILES_WINDOW__", str(FILES_WINDOW))
# Substituted rather than interpolated, because GraphQL is braces from end to end.


# The description and the branch it describes, read together so neither is stale against the other.
Q_CLAIMS = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ headRefOid body }}}
"""

# A `uses:` reference quoted in a description, in the spelling a workflow writes it.
BODY_USES = re.compile(r'uses:\s*(?P<ref>[A-Za-z0-9_.\-]+/[^\s`"\']+@[^\s`"\']+)')
# A commit the description claims this branch carries, being a verb plus a SHA rather than a SHA.
# A bare SHA scan flagged only correct prose, so the verb is what makes it a claim about this branch.
# The vocabulary is an inclusion list, so a phrasing it misses costs a detection and never invents a finding.
BODY_CLAIM = re.compile(
    r"\b(?:fixed|landed|shipped|added|introduced|corrected|resolved|carried|amended)"
    r"\s+(?:in|by|as)\s+`?(?P<sha>[0-9a-f]{7,40})`?",
    re.IGNORECASE,
)
# What `gh` prints when GitHub answered, as opposed to when nothing was reached at all.
HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
# The two GitHub returns for an object that is not there.
# Everything outside this set is "not read" rather than "not there".
ABSENT = {"404", "422"}
# What a reference reads as where GitHub never answered for it.
UNREAD = "unread"
# Longer than the ordinary read's, since this one downloads a repository.
TARBALL_TIMEOUT = 120

# Threads for the reply path, paginated, with `line` read for the confirmation line and not for matching.
Q_THREADS = """
query($o:String!,$r:String!,$n:Int!,$after:String){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    reviewThreads(first:100, after:$after){
      nodes{ id isResolved path line comments(first:1){ nodes{ author{login} body } } }
      pageInfo{ hasNextPage endCursor } }
  }}}
"""

# The PR id for a conversation comment is read in the same run that writes it.
Q_COMMENT_TARGET = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ id url } }}
"""

Q_ATTEST_TARGET = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ headRefOid baseRefName } }}
"""
Q_ATTEST_BRANCH = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ state headRefName headRepository{ url } } }}
"""
LOCAL_REVIEW = Path(__file__).resolve().parent / "local_review.py"

# The conversation-comment and thread mutations the runbook publishes.
# `url` is fetched because it confirms the comment or reply carried a body.
M_COMMENT = """
mutation($subjectId:ID!,$body:String!){
  addComment(input:{subjectId:$subjectId, body:$body}){
    commentEdge{ node{ id url body } } }}
"""
M_REPLY = """
mutation($threadId:ID!,$body:String!){
  addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId, body:$body}){
    comment{ id url body } }}
"""
M_RESOLVE = """
mutation($threadId:ID!){
  resolveReviewThread(input:{threadId:$threadId}){ thread{ id isResolved } }}
"""


def gh_graphql(query: str, **variables) -> dict:
    """Run one GraphQL document and return its `data`, ending the run on a failed read.

    The transport raises `GraphQLError` and never exits, so a failed read prints its detail and
    ends the run at exit 1. Only an error set made of `workflowRun` paths is tolerated.
    """
    try:
        response = shared.request(query, variables, tolerate=_is_workflow_run_error)
    except shared.GraphQLError as exc:
        sys.stderr.write(exc.detail)
        raise SystemExit(str(exc)) from exc
    data = response.data
    if response.tolerated:
        sys.stderr.write("gh graphql: workflowRun unreadable, checks are not deduped by workflow\n")
        data[WORKFLOW_KEYS_UNAVAILABLE] = True
    return data


def _is_workflow_run_error(error: object) -> bool:
    """True where the error is a per-field failure on `workflowRun` and nothing else."""
    path = error.get("path") if isinstance(error, dict) else None
    return isinstance(path, list) and bool(path) and path[-1] == "workflowRun"


def gql(query: str, owner: str, repo: str, num: int) -> dict:
    data = gh_graphql(query, o=owner, r=repo, n=num)
    pr = data["repository"]["pullRequest"]
    if data.get(WORKFLOW_KEYS_UNAVAILABLE) and isinstance(pr, dict):
        pr[WORKFLOW_KEYS_UNAVAILABLE] = True
    return pr


def follow(
    query: str, owner: str, repo: str, num: int, connection: str, after: str | None = None
) -> Iterator[dict]:
    """Each page of the pull request's `connection`, from the one past `after` to the last."""

    def fetch(cursor: str | None) -> dict:
        extra: dict[str, str] = {"after": cursor} if cursor else {}
        return gh_graphql(query, o=owner, r=repo, n=num, **extra)

    try:
        yield from shared.walk(fetch, ("repository", "pullRequest", connection), after)
    except shared.GraphQLError as exc:
        sys.stderr.write(exc.detail)
        raise SystemExit(str(exc)) from exc


def page_files(owner: str, repo: str, num: int, pr: dict) -> None:
    """Read the rest of `pr`'s changed-file list into it, past the full read's first page.

    A list reaching `FILES_CAP` stays marked as cut short, and a page read at another head leaves
    it unread under `FILES_UNREAD`. A list cut short or unread is compared against nothing. A
    failed page read ends the run.
    """
    files = pr.get("files")
    if not isinstance(files, dict):
        return
    page = files.get("pageInfo")
    if not isinstance(page, dict) or page.get("hasNextPage") is not True:
        return
    nodes = files["nodes"]
    for more in follow(Q_FILES, owner, repo, num, "files", page["endCursor"]):
        if more["headRefOid"] != pr.get("headRefOid"):
            files[FILES_UNREAD] = (
                "the head moved while the changed-file list was paged, so the list describes "
                "no one commit"
            )
            return
        nodes.extend(more["files"]["nodes"])
        if len(nodes) >= FILES_CAP:
            return
    files["pageInfo"] = {"hasNextPage": False}


def copilot_history(owner: str, repo: str) -> list[tuple[int, dict]]:
    """The reviewer's own reviews and comments across the most recently updated pull requests.

    Newest activity first, whichever connection it came from. Read at HISTORY_PRS and, where that
    carries no usable bot id, read again at HISTORY_PRS_WIDE, so a long outage or a comment-only
    window does not read as a repository that never saw a Copilot review.
    """
    entries = _copilot_history_window(owner, repo, HISTORY_PRS)
    if entries and copilot_bot_id(entries) is not None:
        return entries
    return _copilot_history_window(owner, repo, HISTORY_PRS_WIDE)


def _copilot_history_window(owner: str, repo: str, prs: int) -> list[tuple[int, dict]]:
    """One read of `copilot_history`'s traversal over the `prs` most recently updated pull
    requests.

    Entries pair with their pull request number and sort by timestamp. Nothing is filtered out, the
    caller's own pull request included. A plain comment is tagged `_kind` so `copilot_bot_id` can
    tell it from a formal review.
    """
    data = gh_graphql(
        Q_BOT_ID, o=owner, r=repo, prs=prs, reviews=HISTORY_REVIEWS, comments=HISTORY_COMMENTS
    )
    nodes = ((data.get("repository") or {}).get("pullRequests") or {}).get("nodes") or []
    entries = []
    for node in nodes:
        number = node.get("number")
        for review in (node.get("reviews") or {}).get("nodes") or []:
            author = review.get("author") or {}
            if author.get("__typename") == "Bot" and author.get("login") == REVIEWER:
                entries.append(history_entry(number, review, "review"))
        for comment in (node.get("comments") or {}).get("nodes") or []:
            if (comment.get("author") or {}).get("login") == REVIEWER:
                entries.append(history_entry(number, comment, "comment"))
    return newest_first(entries)


def request_copilot_review(
    owner: str, repo: str, num: int, pr_node_id: str, bot_id: str | None, settle: float
) -> tuple[str, bool | None]:
    """Ask Copilot to review the current head, and say in one line what happened.

    `union:true` adds the bot to the request set, so a human reviewer stays requested. A failed
    mutation raises as every other write does. Finding no bot id is the one quiet path.

    The second value is `request_recorded` over the state before and after the request, None where
    nothing was requested. An unrecorded answer is read once more `settle` seconds later.
    """
    if not bot_id:
        return (
            (
                f"no Copilot review found across the last {HISTORY_PRS} or, widened once for "
                f"exactly this reason, the last {HISTORY_PRS_WIDE} most-recently-updated pull "
                "requests to read the reviewer bot id from, so nothing was requested here, "
                "polling only. Seed one via the UI if this repository has never had one at all."
            ),
            None,
        )
    before = gh_graphql(Q_REQUEST_STATE, o=owner, r=repo, n=num)["repository"]["pullRequest"]
    answer = gh_graphql(M_REQUEST_REVIEWS, pr=pr_node_id, bot=bot_id)
    after = (answer.get("requestReviews") or {}).get("pullRequest") or {}
    state = backoff(
        after,
        lambda: gh_graphql(Q_REQUEST_STATE, o=owner, r=repo, n=num)["repository"]["pullRequest"],
        lambda p: request_recorded(before, p or {}) is False,
        delays=(settle,),
    )
    recorded = request_recorded(before, state or {})
    line = f"requested a Copilot review on the current head (bot {bot_id})"
    return line, recorded


@functools.cache
def run_cause(owner: str, repo: str, oid: str, before: str) -> tuple[str, str] | None:
    """The error type and rate-limit sentence the reviewer's own failed run logged, or None.

    The run read is the newest failed one on that commit created no later than the review. None
    wherever any read fails or finds nothing. Memoized for the run.
    """
    if not oid or not ISO_STAMP.fullmatch(before):
        return None
    proc = gh_rest(
        f"repos/{owner}/{repo}/actions/runs?event=dynamic&head_sha={oid}&per_page=50",
        f'[.workflow_runs[] | select(.path == "{COPILOT_RUN_PATH}" and .conclusion == "failure"'
        f' and .created_at <= "{before}")] | max_by(.created_at) | .id // empty',
    )
    run = proc.stdout.strip()
    if proc.returncode != 0 or not run.isdigit():
        return None
    proc = gh_rest(f"repos/{owner}/{repo}/actions/runs/{run}/jobs", ".jobs[0].id // empty")
    job = proc.stdout.strip()
    if proc.returncode != 0 or not job.isdigit():
        return None
    proc = gh_rest(f"repos/{owner}/{repo}/actions/jobs/{job}/logs", raw=True)
    if proc.returncode != 0:
        return None
    log = ANSI.sub("", proc.stdout)
    kinds, said = RUN_ERROR_TYPE.findall(log), RUN_RATE_LIMIT.search(log)
    if not kinds:
        return None
    kind = "rate_limit" if "rate_limit" in kinds else kinds[-1]
    return kind, said.group(1).strip() if said else ""


def confirmed_quota(owner: str, repo: str, node: dict) -> str:
    """The run log's rate-limit sentence where an error refusal's run logged a rate limit, else ""."""
    if not possible_quota(node):
        return ""
    cause = run_cause(
        owner, repo, (node.get("commit") or {}).get("oid") or "", node.get("submittedAt") or ""
    )
    if cause is None or cause[0] != "rate_limit":
        return ""
    return cause[1] or "the run logged a rate limit and stated no reset time"


def full_read(owner: str, repo: str, num: int, pr: dict | None = None) -> dict:
    """The full payload with its changed files paged, read again while that list is unread."""

    def read() -> dict:
        fresh = gql(Q_FULL, owner, repo, num)
        page_files(owner, repo, num, fresh)
        return fresh

    if pr is None:
        pr = read()
    else:
        page_files(owner, repo, num, pr)
    return backoff(pr, read, files_unread, delays=READ_RETRY_DELAYS)


def reread_json(path: str, jq: str) -> object:
    """One REST read whose filter emits JSON, parsed, re-read on the `READ_RETRY_DELAYS` schedule.

    None wherever nothing parseable came back, an absence GitHub answered included.
    """
    return parsed_json(reread(path, jq))


def reread(path: str, jq: str) -> subprocess.CompletedProcess:
    """The last of `reread_json`'s reads, whole, so a caller can say why nothing parsed."""
    read = functools.partial(gh_rest, path, jq)
    return backoff(
        read(),
        read,
        lambda p: not answered_absent(p) and parsed_json(p) is None,
        delays=READ_RETRY_DELAYS,
    )


def parsed_json(proc: subprocess.CompletedProcess) -> object:
    """The read's JSON, or None where it failed or printed nothing parseable."""
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return None


def required_for(
    pr: dict, owner: str, repo: str, gated: frozenset[str] = frozenset({"BLOCKED"})
) -> frozenset[tuple[str, int | None]]:
    """The required entries a merge in `gated` can be short of, empty wherever none can matter."""
    if pr.get("state", "OPEN") != "OPEN" or pr.get("mergeStateStatus") not in gated:
        return frozenset()
    return required_contexts(owner, repo, pr.get("baseRefName") or "") or frozenset()


@functools.cache
def required_contexts(owner: str, repo: str, base: str) -> frozenset[tuple[str, int | None]] | None:
    """Every status check the base branch's rules require, or None where it was unread.

    Each is a (context, integration id) pair, the id None where the rule binds no app. Read from
    the rules, since the rollup lists only registered checks. None wherever the read did not
    happen, a malformed integration id, or a full page of a hundred rules. Memoized for the run.
    """
    safe = base and not UNSAFE_REF.search(base) and not DOT_SEGMENT.search(base)
    read = (
        reread_json(
            f"repos/{owner}/{repo}/rules/branches/{urllib.parse.quote(base, safe='')}?per_page=100",
            '{n: length, c: [.[] | select(.type=="required_status_checks")'
            " | .parameters.required_status_checks[] | [.context, .integration_id]]}",
        )
        if safe
        else None
    )
    answer = read if isinstance(read, dict) else {}
    entries = answer.get("c")
    if (
        not isinstance(entries, list)
        or not all(
            isinstance(e, list)
            and len(e) == 2
            and isinstance(e[0], str)
            and e[0]
            and (
                e[1] is None or (isinstance(e[1], int) and not isinstance(e[1], bool) and e[1] > 0)
            )
            for e in entries
        )
        or answer.get("n", 100) >= 100
    ):
        print(
            f"note: the rules for {base} were unread, so which checks they require is unknown and "
            "this wait cannot judge one that has not registered."
        )
        return None
    return frozenset((e[0], e[1]) for e in entries)


def liveness(pr: dict, min_rounds: int = 0) -> tuple[bool, dict | None, list[str]]:
    """Return (head_review_done, answered_outside_review, reviewer_login_drift) off one payload.

    The three readings `wait`'s Copilot poll stops on, shared by its first evaluation and every
    re-read. `Q_LIVE` carries the authors, so drift costs no extra call. A `min_rounds` of 0 reads
    as `reviewed_head`.
    """
    return head_review_done(pr, min_rounds), answered_outside_review(pr), reviewer_login_drift(pr)


def live_state(
    owner: str, repo: str, num: int, min_rounds: int = 0
) -> tuple[bool, dict | None, list[str]]:
    """Return `liveness` off one fresh `Q_LIVE`, since a push during the wait moves the head."""
    return liveness(gql(Q_LIVE, owner, repo, num), min_rounds)


def digest(
    owner: str,
    repo: str,
    num: int,
    seen: set[str] | None = None,
    pr: dict | None = None,
    now: datetime | None = None,
    grace: float = CHECK_GRACE,
    stall: float = CHECK_STALL,
    checks: list[dict] | None = None,
) -> tuple[str, int]:
    """Render the digest from a caller's payload and normalized checks when supplied.

    The caller passes its own readings so the exit code agrees with what was printed. `now` is a
    parameter so a case can hold a check at a known age.
    """
    pr = full_read(owner, repo, num) if pr is None else pr
    now = datetime.now(UTC) if now is None else now
    head = pr["headRefOid"]
    revs = reviewer_nodes(pr, "reviews")
    on_head = head_reviews(pr)
    newest = newest_of(on_head)
    found = reading(newest, pr=pr) if newest is not None else None
    effort, effort_source = review_effort(pr)
    cover, cover_why = coverage(pr)
    hold = shape_hold(pr)
    # Every round's notes, since a renamed section on an earlier round hides a finding too.
    notes = [
        *dict.fromkeys(
            f"round {((node.get('commit') or {}).get('oid') or 'unknown')[:8]}: {n}"
            for node in revs
            for n in reading(node).notes
            if node not in on_head or n not in {p.note for p in reading(node).unplaced}
        ),
        *reviewer_login_drift(pr),
    ]
    threads = pr["reviewThreads"]["nodes"]
    truncated = threads_truncated(pr)
    revs_truncated = reviews_truncated(pr)
    unresolved = open_threads(threads)
    by_login = {
        login: sum(1 for t in unresolved if thread_author(t) == login) for login in KNOWN_REVIEWERS
    }
    by_login["other"] = sum(1 for t in unresolved if thread_author(t) not in KNOWN_REVIEWERS)
    contributors = {login: n for login, n in by_login.items() if n}
    breakdown = (
        " (" + " ".join(f"{login}={n}" for login, n in contributors.items()) + ")"
        if len(contributors) > 1 or "other" in contributors
        else ""
    )
    suppressed, suppressed_field = collected(pr, REVIEWER, "suppressed")
    missed, missed_field = collected(pr, REVIEWER, "previously_missed")
    outside, outside_field = collected(pr, CODERABBIT_LOGIN, "previously_missed")
    listed = len(opened_by(pr, newest)) if newest is not None else 0
    flagged = uncounted_verdict(pr)
    advisory = uncounted_verdict(pr, "clean")
    answer = answered_outside_review(pr)
    refusal = stopping_refusal(pr) or (None if on_head else refusing_review(pr))
    refusal_field = (
        "no"
        if not refusal
        else "QUOTA"
        if quota_refusal(refusal) or confirmed_quota(owner, repo, refusal)
        else "ERROR"
        if possible_quota(refusal)
        else "YES"
    )
    blind = [f for f in ("reviews", "comments") if window_blind(pr, f)]
    answered = "yes" if answer else ("unknown" if blind else "no")
    checks = check_nodes(pr) if checks is None else checks
    ok, total = checks_tally(checks)
    stuck = checks_stuck(checks, now, grace, stall)
    other_on_head = [
        login
        for login in OTHER_REVIEWERS
        if any(
            (n.get("commit") or {}).get("oid") == head for n in reviewer_nodes(pr, "reviews", login)
        )
    ]
    other_limited = {
        login: name for login in OTHER_REVIEWERS if (name := rate_limited_by(pr, login)) is not None
    }
    qodo = qodo_comment(pr)
    qodo_open = [s for s in reading(qodo, QODO_LOGIN).sections if s.kind == "open"] if qodo else []
    lines = [
        # The repository leads the line, since a digest of the wrong pull request is well-formed.
        f"repo={owner}/{repo} pr={num} head={head[:8]} rounds={len(revs)} "
        f"review_on_head={'yes' if on_head else 'local' if cover == 'local' else 'NO'} "
        + (f"other_reviewed={','.join(other_on_head)} " if other_on_head else "")
        + (
            "other_rate_limited="
            + ",".join(
                f"{login}:{name}" if name else login for login, name in other_limited.items()
            )
            + " "
            if other_limited
            else ""
        )
        + f"effort={effort} effort_source={effort_source} "
        f"coverage={cover.upper() if cover in ('partial', 'unknown') else cover} "
        f"shapes={('ACKNOWLEDGED' if hold[2] else 'HELD') if hold else 'NOTED' if notes else 'ok'} "
        f"refusal={refusal_field} "
        f"threads={len(threads)}{'+' if truncated else ''} "
        f"unresolved={len(unresolved)}{'+' if truncated else ''}{breakdown} "
        f"suppressed={suppressed_field} "
        + (f"previously_missed={missed_field} " if missed or revs_truncated else "")
        + (
            f"overview={'?' if found.open is None else found.open}/{listed} "
            if found is not None and found.marked
            else ""
        )
        + (f"cr_outside_diff={outside_field} " if outside or revs_truncated else "")
        + (
            f"qodo_open={len(qodo_open)} "
            if qodo
            else ("qodo_open=unknown " if qodo_comments_blind(pr) else "")
        )
        + f"answered_outside_review={answered} "
        f"requested={'yes' if reviewer_requested(pr) else 'no'} "
        f"merge={pr.get('mergeStateStatus')} "
        f"checks={ok}/{total}"
        + (f" stuck={','.join(sorted({s for _, s in stuck}))}" if stuck else "")
    ]
    if refusal:
        lines.append(
            "  COPILOT REFUSED THIS ROUND: the newest Copilot review on this pull request says "
            "it did not review, so it covers nothing and re-requesting repeats it, and the body "
            "below is what says which remedy applies"
        )
        lines += [
            f"    {ln.rstrip()}" for ln in (refusal.get("body") or "").splitlines() if ln.strip()
        ]
        if limit := confirmed_quota(owner, repo, refusal):
            lines.append(f"  RATE LIMIT FROM THE REVIEWER'S RUN LOG: {limit}")
    if notes:
        lines.append(
            f"  REVIEWER OUTPUT NOTED ({len(notes)}): the reader placed part of what the reviewer "
            "sent nowhere, so read the body itself before closing the review loop. A spelling "
            f"to add belongs in `scripts/reviewers/` on {HUB}, which hosts this script"
        )
        lines += [f"    {note}" for note in notes]
    if hold:
        lines.append(
            f"  UNPLACED ON THE HEAD (fingerprint={hold[0]}): a round covering the head carries "
            + (
                "parts an owner, member, or collaborator acknowledged with written outcomes"
                if hold[2]
                else "text the reader cannot place, where a finding can sit. Read each part whole "
                "in the review body, the excerpt only locating it, and give each item an outcome, "
                "fixed with its SHA, declined with evidence, or listed as pre-existing in the PR "
                f"body. Post them with `comment {num} --repo {owner}/{repo} --fingerprint "
                f"{hold[0]} --body <outcomes>`"
            )
        )
        for part in hold[1]:
            lines += [f"    {part.note}", *([] if hold[2] else adapter.excerpt(part.text))]
    if found is not None and found.unthreaded:
        lines.append(
            f"  FINDINGS WITH NO THREAD ({found.unthreaded}): the round covering the head names "
            f"more findings than its open entries link to threads on this pull request, so "
            f"{found.unthreaded} {'is' if found.unthreaded == 1 else 'are'} raised where polling "
            "threads cannot see them. Read the review body and give each the triage a suppressed "
            "finding gets. A thread past the hundred read here counts too, and `threads=` "
            "carries a trailing `+` where that page was cut"
        )
    if flagged:
        lines.append(
            f"  VERDICT WITH NO COUNTED FINDING: the round covering the head opens on "
            f"`{flagged[0]}` and counts no finding, so its headline is the one place what it "
            "flags is named. Give the headline the triage a suppressed finding gets"
        )
        lines.append(f"    {flagged[1] or '(the headline carries no prose)'}")
    if advisory and advisory[1]:
        lines.append(
            f"  CLEAN VERDICT HEADLINE: the round covering the head opens on `{advisory[0]}` and "
            "counts no finding. Its headline may name a remaining nit, new or already answered, "
            "which nothing here tells apart, so this is advisory and changes no exit code"
        )
        lines.append(f"    {advisory[1]}")
    if cover in ("table", "carried:table", "local", "partial", "unknown"):
        lines.append(f"  COVERAGE IS {cover.upper()}: {cover_why}")
    elif cover == "none" and on_head:
        lines.append(
            "  NO COVERAGE IS SHOWN: no round covering the head states coverage, and "
            f"{cover_why or 'nothing stands in'}. Merge Gate item 2 reads this field"
        )
    lines.extend(check_lines(pr, checks, stuck, now))
    if blind:
        lines.append(
            f"  BEHIND THE WINDOW ({' and '.join(blind)}): the newest {WINDOW} carry "
            "none from the reviewer and older ones exist, so this cannot decide"
        )
    if truncated:
        lines.append(
            "  THREADS TRUNCATED: this pull request carries more review threads than the "
            "100 read here, and the connection reads oldest-first, so the ones cut off are "
            "the newest rather than the oldest, exactly the ones most likely to still be "
            "open. `threads=` and `unresolved=` above undercount. Read the rest with "
            "`reply`'s own paginated walk before trusting either number"
        )
    if revs_truncated:
        lines.append(
            "  REVIEWS TRUNCATED: this pull request carries more reviews than the 100 read "
            "here, and the connection reads newest-first, so the ones cut off are the "
            "oldest, exactly where an unanswered suppressed or outside-diff-range finding is "
            "most likely to still sit unresolved. `suppressed=` and `cr_outside_diff=` above "
            "undercount"
        )
    if answer:
        # Printed whole, since its wording is the only thing separating a refusal from a remark.
        lines.append(
            f"  COPILOT COMMENT ({answer.get('createdAt')}, newer than any review): "
            "the reviewer answered without reviewing, so read the body below and "
            "decide, since a refusal is terminal and a remark is not"
        )
        lines += [
            f"    {ln.rstrip()}" for ln in (answer.get("body") or "").splitlines() if ln.strip()
        ]
    new = 0
    for t in unresolved:
        c = first_comment(t)
        tid = t["id"]
        mark = ""
        if seen is not None:
            if tid in seen:
                continue
            seen.add(tid)
            mark = "NEW "
            new += 1
        body = " ".join((c.get("body") or "").split())
        lines.append(f"  {mark}{tid} {c.get('path')}:{c.get('line')} {body[:160]}")
    # Printed whole, since a finding with no thread appears nowhere but here.
    for heading, entries in (
        ("SUPPRESSED", suppressed),
        ("PREVIOUSLY MISSED", missed),
        ("CODERABBIT OUTSIDE-DIFF", outside),
    ):
        for where, section in entries:
            lines.append(
                f"  {heading} ({where}): no thread to resolve, "
                "answer it in the PR conversation quoting the finding"
            )
            lines += [f"    {ln}" for ln in section.text.splitlines() if ln.strip()]
    for section in qodo_open:
        lines.append(
            "  QODO OPEN FINDING: neither Resolved nor Dismissed, no thread to resolve, "
            "answer it in the PR conversation quoting the finding, and spot-verify Qodo's "
            "own badge against `gh pr diff` rather than trusting it outright"
        )
        lines.append(f"    {section.title}")
    if seen is not None:
        lines[0] += f" new={new}"
    return "\n".join(lines), len(unresolved)


def report_verdict(pr: dict) -> int:
    """Print the blocking verdict's own status line, and return the exit code it carries.

    A missing head review has no verdict yet, which the wait path owns. Coverage shown nowhere is a
    note rather than an exit. A head round's unplaced part is an exit until it is acknowledged.
    """
    if not head_reviews(pr):
        return 0
    state, why = coverage(pr)
    if state == "partial":
        if why.startswith(CARRIED_PARTIAL):
            print(
                f"status=COVERAGE_IS_PARTIAL the head reads as partial because {why}, so part of "
                "the diff may have no review. A round on the head stating full coverage clears "
                "this, unless a round on it already states a reading this cannot believe. "
                "Otherwise this is the maintainer's call"
            )
        else:
            print(
                f"status=COVERAGE_IS_PARTIAL the review covering the head states it read fewer "
                f"files than the pull request changed ({why}), so part of the diff has no review "
                "at all. A re-request is not the remedy it reads as. Splitting is the remedy where "
                "it applies, and it does not apply to a promotion. Otherwise this is the "
                "maintainer's call"
            )
        return 42
    if flagged := uncounted_verdict(pr):
        print(
            f"status=VERDICT_NAMES_UNCOUNTED_FINDINGS the round covering the head opens on "
            f"`{flagged[0]}`, a verdict that is not the clean one, while it counts no finding, so "
            "what it flags is named in its headline alone, printed above under VERDICT WITH NO "
            "COUNTED FINDING. Fix what it names and push, or answer it with `comment`. This code "
            "repeats on this head after the answer, since nothing here reads one"
        )
    if (hold := shape_hold(pr)) and not hold[2]:
        print(
            f"status=UNPLACED_PARTS_UNACKNOWLEDGED the round covering the head carries text the "
            f"reader cannot place, under UNPLACED ON THE HEAD. Acknowledging fingerprint {hold[0]} "
            "with `comment --fingerprint` and each item's outcome clears this code"
        )
    if flagged or (hold and not hold[2]):
        return 50
    if state in ("none", "unknown"):
        print(
            "note: no statement, file table, or attested local pass shows full coverage of this "
            "head, so Merge Gate item 2 is unmet unless the promotion case it states applies"
        )
    return 0


def origin_owner() -> str | None:
    """The owner `git remote get-url origin` yields against this script's own directory, or None."""
    # An inherited GIT_DIR, GIT_COMMON_DIR, or GIT_CONFIG* name can point the probe at another repository's config or inject an `insteadOf` rewrite.
    # The environment is stripped rather than cleared, since git needs PATH and HOME.
    # An inherited HOME, XDG_CONFIG_HOME, or PATH is not closable here, since its holder already controls the writer.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")
        and not k.startswith("GIT_CONFIG")
    }
    try:
        url = subprocess.run(
            ["git", "-C", str(HERE), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
            env=env,
        ).stdout.strip()
    # A missing git, a timeout, or any other failure all mean the owner cannot be read.
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$", url)
    return m.group(1).lower() if m else None


def in_scope(target_owner: str) -> tuple[bool, str]:
    """Whether writing to `target_owner` is in scope here, with the reason where it is not.

    The same owner covers the origin and every sibling. A different owner is refused outright
    rather than granted by an environment variable, since the caller could set it for itself. The
    cross-owner case belongs on the runbook's explicit `gh` path, where the `gh-write-guard` hook
    adjudicates it.
    """
    origin = origin_owner()
    if origin is None:
        return False, (
            "this script got no owner from `git remote get-url origin` against the directory "
            f"it sits in, {HERE}, so the owner a write would stay within cannot be "
            "established, and an unverified scope is not a scope. That directory is this "
            "script's own rather than the working directory, so a readable `origin` in the "
            "working directory says nothing about this, and the remedy for a copy of this "
            "script sitting outside a checkout is to run the copy that lives inside a hub "
            "checkout instead of this one"
        )
    if target_owner.lower() != origin:
        return False, (
            f"the target is under {target_owner}, and `git remote get-url origin`, run against "
            f"the directory this script sits in, {HERE}, named {origin}. A different owner is "
            "the shape this refuses outright: take it through the runbook mutations, where the "
            "write-guard hook reads the maintainer grant"
        )
    return True, ""


def unresolved_threads(owner: str, repo: str, num: int) -> list[dict]:
    """Every unresolved review thread, following the cursor to the end."""
    out: list[dict] = []
    for pr in follow(Q_THREADS, owner, repo, num, "reviewThreads"):
        out += open_threads(pr["reviewThreads"]["nodes"])
    return out


def comment_on_pr(owner: str, repo: str, num: int, body: str) -> int:
    """Post one PR conversation comment and confirm the returned comment. Returns an exit code."""
    ok, why = in_scope(owner)
    if not ok:
        print(f"status=OUT_OF_SCOPE nothing was written: {why}")
        return 64

    target = gql(Q_COMMENT_TARGET, owner, repo, num)
    if not target or not target.get("id") or not target.get("url"):
        print(
            f"status=TARGET_NOT_READ nothing was written: {owner}/{repo} #{num} did not "
            "return a pull request id and URL, so the mutation has no verified target"
        )
        return 65

    body = body.replace("\r\n", "\n").replace("\r", "\n")
    edge = (gh_graphql(M_COMMENT, subjectId=target["id"], body=body).get("addComment") or {}).get(
        "commentEdge"
    ) or {}
    comment = edge.get("node") or {}
    if not comment.get("url") or (comment.get("body") or "") != body:
        print(
            "status=COMMENT_NOT_CONFIRMED the response returned no URL or a different body. "
            "Inspect the PR conversation before retrying, since the write may have landed."
        )
        print(f"  response: {json.dumps(comment)[:400]}")
        return 66
    print(f"commented: {comment['url']}")
    print("status=COMMENTED")
    return 0


def attest(owner: str, repo: str, num: int, checkout: str) -> int:
    """Publish that a recorded local pass covers this pull request's head. Returns an exit code.

    The `local_review.py` receipt lives in the checkout's git directory, which GitHub cannot read,
    so this posts a comment the gate can read. It first checks that the checkout's HEAD is the pull
    request's head, the checkout holds no change beyond that commit, its merge base is the pull
    request's own, and `local_review.py status` reports a current pass against the base. The first
    two are read again after the check returns.
    """
    ok, why = in_scope(owner)
    if not ok:
        print(f"status=OUT_OF_SCOPE nothing was written: {why}")
        return 64
    target = gql(Q_ATTEST_TARGET, owner, repo, num) or {}
    head, base = target.get("headRefOid") or "", target.get("baseRefName") or ""
    if not head or not base:
        print(
            f"status=TARGET_NOT_READ nothing was written: {owner}/{repo} #{num} did not return "
            "its head commit and base branch"
        )
        return 65
    local = _git(checkout, "rev-parse", "HEAD")
    dirty = _git(
        checkout, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"
    )
    if local.returncode != 0 or dirty.returncode != 0:
        print(
            f"status=CHECKOUT_NOT_READ nothing was written: {checkout} is not a readable checkout"
        )
        return 67
    registered = True
    if local.stdout.strip() != head:
        head, registered = await_registered_head(
            owner, repo, num, checkout, local.stdout.strip(), head
        )
    if local.stdout.strip() != head:
        print(
            f"status=CHECKOUT_NOT_THE_HEAD nothing was written: the checkout is at "
            f"{local.stdout.strip()[:8]} and the pull request's head is {head[:8]}, so push or "
            "fetch until they agree"
            + ("" if registered else ". The push landed and GitHub had not registered it yet")
        )
        return 67
    if dirty.stdout.strip():
        print(
            "status=CHECKOUT_NOT_THE_HEAD nothing was written: the checkout holds changes the "
            "head commit does not, so a pass over it does not describe what was pushed"
        )
        return 67
    if UNSAFE_REF.search(base) or DOT_SEGMENT.search(base):
        print(
            f"status=MERGE_BASE_NOT_READ nothing was written: the base branch {base!r} carries a "
            "character that would change the compare path rather than travel along it"
        )
        return 67
    ours = _git(checkout, "merge-base", f"refs/remotes/origin/{base}", "HEAD")
    theirs = gh_rest(
        f"repos/{owner}/{repo}/compare/{base}...{head}", ".merge_base_commit.sha // empty"
    )
    if ours.returncode != 0 or theirs.returncode != 0 or not theirs.stdout.strip():
        print(
            f"status=MERGE_BASE_NOT_READ nothing was written: the merge base with {base} could "
            "not be read in the checkout or from GitHub, so the pass's scope cannot be compared "
            "with the pull request's"
        )
        print(f"  {(ours.stderr or theirs.stderr).strip()[:400]}")
        return 67
    if ours.stdout.strip() != theirs.stdout.strip():
        print(
            f"status=CHECKOUT_NOT_THE_HEAD nothing was written: the checkout's merge base with "
            f"origin/{base} is not the pull request's own, so a pass against it measured a "
            "different change set. Fetch, or attest from a checkout of the pull request's own "
            "repository"
        )
        return 67
    covered, findings, said = receipt_reading(checkout, base)
    if not covered:
        print(
            f"status=NO_LOCAL_PASS nothing was written: `local_review.py status --target {base}` "
            "reports no recorded pass covering this content. Run the local strict review, "
            "record it, and attest again"
        )
        print(f"  {said.strip()[:400]}")
        return 68
    after = _git(checkout, "rev-parse", "HEAD")
    still = _git(
        checkout, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"
    )
    if after.stdout.strip() != head or still.returncode != 0 or still.stdout.strip():
        print(
            "status=CHECKOUT_NOT_THE_HEAD nothing was written: the checkout moved or changed "
            "while the pass was being checked, so the check no longer describes the head"
        )
        return 67
    body = (
        f"A recorded local strict-review pass covers head `{head}`, the content this pull "
        f"request carries at that commit against `{base}`, and it recorded {findings} "
        f"finding{'' if findings == '1' else 's'}.\n\n"
        f"<!-- fleet-local-review: head={head} base={base} findings={findings} -->"
    )
    return comment_on_pr(owner, repo, num, body)


def await_registered_head(
    owner: str, repo: str, num: int, checkout: str, local: str, head: str
) -> tuple[str, bool]:
    """The pull request's head after waiting for GitHub to register a push, and whether it did.

    Applies only where the remote branch tip is the checkout's own commit, so the head is merely
    late. Any other tip, or one that cannot be read, returns at once with the head as read.
    """
    meta = gql(Q_ATTEST_BRANCH, owner, repo, num) or {}
    ref, source = meta.get("headRefName") or "", (meta.get("headRepository") or {}).get("url") or ""
    if (
        meta.get("state") != "OPEN"
        or not ref
        or not source
        or UNSAFE_REF.search(ref)
        or DOT_SEGMENT.search(ref)
    ):
        return head, True
    tip = _git(checkout, "ls-remote", source, f"refs/heads/{ref}")
    if tip.returncode != 0 or tip.stdout.split()[:1] != [local]:
        return head, True
    first = head
    for delay in ATTEST_HEAD_DELAYS:
        _attest_sleep(delay)
        head = (gql(Q_ATTEST_TARGET, owner, repo, num) or {}).get("headRefOid") or head
        if head == local:
            return head, True
        if head != first:
            return head, True
    return head, False


def receipt_reading(checkout: str, base: str) -> tuple[bool, str, str]:
    """Whether a recorded pass covers the checkout's content, its findings, and what was read.

    One `local_review.py status` read answers both, so coverage and count describe the same
    receipt. The findings are the covering passes' counts summed, or "unknown" where any recorded
    none, and are shown rather than gated.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(LOCAL_REVIEW), "status", "--target", base],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "unknown", "local_review.py could not be run"
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return False, "unknown", proc.stdout or proc.stderr
    if proc.returncode != 0 or not isinstance(data, dict) or data.get("covered") is not True:
        return False, "unknown", proc.stdout or proc.stderr
    if data.get("receiptProblems"):
        return False, "unknown", proc.stdout
    counts = data.get("findings")
    if (
        not isinstance(counts, dict)
        or not counts
        or not all(isinstance(n, int) and n >= 0 for n in counts.values())
    ):
        return True, "unknown", proc.stdout
    return True, str(sum(counts.values())), proc.stdout


def _git(checkout: str, *args: str) -> subprocess.CompletedProcess:
    """One read-only git command in `checkout`, returned whole rather than raised."""
    try:
        return subprocess.run(
            ["git", "-C", checkout, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess([], 1, "", "git could not be run")


POLL_DELAYS = (15, 20, 30, 45, 60, 120)


def backoff[Polled](
    value: Polled,
    read: Callable[[], Polled],
    keep: Callable[[Polled], bool],
    start: float | None = None,
    timeout: float = 0.0,
    delays: Iterable[float] | None = None,
) -> Polled:
    """Re-read `value` on a schedule of delays while `keep` holds and the bound allows.

    `delays` defaults to `POLL_DELAYS`, its last delay repeated only where a clock bounds the loop.
    A finite schedule ends by running out, with no clock where `start` is None. Otherwise `start`
    is a `time.monotonic` reading and `timeout` is in seconds, checked before each sleep. Returns
    the last value read, or `value` itself where the poll never sleeps.
    """
    if delays is None:
        repeated = itertools.repeat(POLL_DELAYS[-1]) if start is not None else ()
        delays = itertools.chain(POLL_DELAYS, repeated)
    for delay in delays:
        if not keep(value) or (start is not None and time.monotonic() - start > timeout):
            break
        time.sleep(delay)
        value = read()
    return value


def reply_to_thread(
    owner: str,
    repo: str,
    num: int,
    match: str,
    body: str,
    path: str | None,
    resolve: bool,
    all_identical: bool = False,
) -> int:
    """Answer the one thread `match` selects, and resolve it where asked. Returns an exit code.

    Every refusal is a stop rather than a fallback: no guessed id, no second-best thread, and no
    resolve on a reply that did not land. A no-match names the unresolved count.

    `all_identical` covers a reviewer posting the same finding as two threads. It answers every
    candidate with the one body, only when they all sit on one file and fold identically.
    """
    ok, why = in_scope(owner)
    if not ok:
        print(f"status=OUT_OF_SCOPE nothing was written: {why}")
        return 64

    threads = unresolved_threads(owner, repo, num)
    hits = matching_threads(threads, match, path)
    if not hits:
        print(
            f"status=NO_MATCH nothing was written: no unresolved thread on {owner}/{repo} "
            f"#{num} carries {match!r}"
            + (f" at {path}" if path else "")
            + f", of {len(threads)} unresolved thread(s) total"
            + ". Widen the words or drop --path rather than reaching for an id, since the "
            "thread may also be resolved already, which reads the same from here."
        )
        for t in threads:
            print(f"  unresolved: {describe(t)}")
        return 60
    paths = {t.get("path") for t in hits}
    identical = len(paths) == 1 and len({finding_text(t) for t in hits}) == 1
    if len(hits) > 1 and not (all_identical and identical):
        if len(paths) > 1:
            hint = "The candidates sit on different files, so add --path to select one."
        elif identical:
            hint = (
                "Every candidate opens with the same text, so no wording selects one: pass "
                "--all-identical to answer each of them with this body."
            )
        elif all_identical:
            hint = (
                "--all-identical answers only candidates whose opening comments read the same, "
                "and these differ, so quote more of the finding."
            )
        else:
            hint = "Quote more of the finding, or add --path."
        print(
            f"status=AMBIGUOUS nothing was written: {len(hits)} unresolved threads carry "
            f"{match!r}, and picking one of them is the failure this avoids rather than a "
            f"default it can take. {hint}"
        )
        for t in hits:
            print(f"  candidate: {describe(t)}")
        return 61

    for done, target in enumerate(hits):
        code = answer_thread(target, body, resolve)
        if code:
            if len(hits) > 1:
                replied = done + (code == 63)
                print(
                    f"  {replied} of {len(hits)} identical threads are confirmed to carry this reply, "
                    "and none after this one was attempted."
                )
            return code

    count = f" ({len(hits)} identical threads)" if len(hits) > 1 else ""
    if not resolve:
        print(
            f"status=REPLIED{count} answered and left open, since --resolve was not given. "
            "A decline is resolved only once its evidence is in the thread."
        )
        return 0
    print(f"status=REPLIED_AND_RESOLVED{count}")
    return 0


def answer_thread(target: dict, body: str, resolve: bool) -> int:
    """Reply to one selected thread and resolve it where asked. Returns 0, or an exit code."""
    print(f"answering: {describe(target)}")
    reply = (
        gh_graphql(M_REPLY, threadId=target["id"], body=body).get("addPullRequestReviewThreadReply")
        or {}
    )
    comment = reply.get("comment") or {}
    # The url says a reply carried a body, and resolving past an empty one closes the thread with nothing in it.
    if not comment.get("url") or not (comment.get("body") or "").strip():
        print(
            "status=REPLY_NOT_CONFIRMED the reply returned no url or an empty body, so the "
            "thread is NOT resolved and the answer is not recorded. Read the response above "
            "before retrying, since a write that appears to fail may have taken on the server."
        )
        print(f"  response: {json.dumps(reply)[:400]}")
        return 62
    print(f"replied: {comment['url']}")

    if not resolve:
        return 0

    thread = (gh_graphql(M_RESOLVE, threadId=target["id"]).get("resolveReviewThread") or {}).get(
        "thread"
    ) or {}
    if not thread.get("isResolved"):
        print(
            "status=RESOLVE_NOT_CONFIRMED the reply landed and the resolve did not report the "
            "thread resolved, so it is still open and the answer is already posted."
        )
        print(f"  response: {json.dumps(thread)[:400]}")
        return 63
    return 0


def gh_rest(path: str, jq: str | None = None, raw: bool = False) -> subprocess.CompletedProcess:
    """One REST read, returned whole so the caller can tell an absent object from an unread one.

    Unlike `gh_graphql` this does not raise on a non-zero exit, since a 404 is an answer. Every
    path passed in is a GET. `raw` is for a job log, which `gh` 2.97 and later refuse to print
    without `--allow-escape-sequences`, so the flag is passed only where `gh_takes_escape_flag`
    says so.
    """
    argv = ["gh", "api", path] + (["--jq", jq] if jq else [])
    if raw and gh_takes_escape_flag():
        argv.append("--allow-escape-sequences")
    return _gh_run(argv, raw)


@functools.cache
def gh_takes_escape_flag() -> bool:
    """Whether this `gh` accepts `--allow-escape-sequences`, answered by its own exit status."""
    return _gh_run(["gh", "api", "--allow-escape-sequences", "--help"], False).returncode == 0


def _gh_run(argv: list[str], raw: bool) -> subprocess.CompletedProcess:
    """Run one `gh` read, a log decoded leniently since its bytes are the runner's own."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace" if raw else "strict",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 1, "", "gh could not be run")


def answered_absent(proc: subprocess.CompletedProcess) -> bool:
    """Whether GitHub said the object is not there, as opposed to nothing having been read."""
    m = HTTP_STATUS.search(proc.stderr)
    return m is not None and m.group(1) in ABSENT


def body_references(body: str) -> tuple[list[str], list[str]]:
    """The `uses:` refs and the claimed commits a description quotes, de-duplicated and sorted.

    A commit counts only where a verb claims this branch carries it, since a SHA otherwise mentions
    history, another repository, or quoted output. Prose claims stay out, as judging them needs a
    similarity heuristic. A claimed SHA must still carry a digit, so an all-hex English word such
    as `accede` is never read as a commit.
    """
    uses = sorted({m.group("ref") for m in BODY_USES.finditer(body)})
    shas = sorted(
        {
            m.group("sha")
            for m in BODY_CLAIM.finditer(body)
            if any(c.isdigit() for c in m.group("sha"))
        }
    )
    return uses, shas


def commit_state(owner: str, repo: str, sha: str, head: str) -> str:
    """The empty string where `head` carries `sha`, `UNREAD` where nothing was read, else why not."""
    proc = gh_rest(f"repos/{owner}/{repo}/compare/{sha}...{head}", ".status")
    if proc.returncode == 0:
        status = proc.stdout.strip()
        if status in ("identical", "ahead"):
            return ""
        return (
            f"{owner}/{repo} carries the commit and this head does not descend from it ({status})"
        )
    if answered_absent(proc):
        return f"{owner}/{repo} carries no such commit"
    return UNREAD


def head_carries(owner: str, repo: str, head: str, refs: list[str]) -> set[str] | None:
    """Which of `refs` appear anywhere in the tree at `head`, or None where nothing was read.

    Read at the head commit, since the branch need not be fetched here. The whole tree is searched,
    since `uses:` lines also sit in catalog snippets and docs, and one archive is one request.
    """
    # Read as bytes, so it carries its own guards and an absent `gh` or hung download reads as undecided.
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/tarball/{head}"],
            capture_output=True,
            timeout=TARBALL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    needles = {r: r.encode() for r in refs}
    found: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                # Matched as bytes, so an undecodable file is searched rather than skipped.
                blob = handle.read()
                found |= {r for r, n in needles.items() if r not in found and n in blob}
                if len(found) == len(needles):
                    break
    except (tarfile.TarError, OSError, EOFError):
        return None
    return found


def check_claims(owner: str, repo: str, num: int) -> int:
    """Report every reference the description makes that its own head tree does not carry."""
    pr = gql(Q_CLAIMS, owner, repo, num)
    head, body = pr["headRefOid"], pr.get("body") or ""
    uses, shas = body_references(body)

    stale, unread = [], 0
    for sha in shas:
        state = commit_state(owner, repo, sha, head)
        if state == UNREAD:
            unread += 1
        elif state:
            stale.append(
                f"STALE COMMIT `{sha}`: {state}, so the description points at something "
                "this branch does not have"
            )

    # One read for every ref together, since the archive it reads is the same one either way.
    carried = head_carries(owner, repo, head, uses) if uses else set()
    for ref in uses:
        if carried is None:
            unread += 1
        elif ref not in carried:
            stale.append(f"STALE USES `{ref}`: no file at this head carries that ref")

    print(
        f"repo={owner}/{repo} pr={num} head={head[:8]} commits={len(shas)} uses={len(uses)} "
        f"stale={len(stale)} unread={unread}"
    )
    for line in stale:
        print(f"  {line}")
    if stale:
        # Named as the description's problem, since the branch is the ground truth.
        print(
            "status=DESCRIPTION_CONTRADICTS_ITS_BRANCH update the body to what the head tree "
            "carries, since a reference that resolves to nothing is caught by a reviewer or "
            "not at all"
        )
        return 70
    if unread and unread == len(shas) + len(uses):
        # A check that decided nothing prints the same `stale=0` a clean one does.
        print(
            "status=NOTHING_WAS_READ every reference in the description was left undecided "
            "because GitHub did not answer for any of them, so this reports no verdict"
        )
        return 71
    if unread:
        print(f"  NOTE: {unread} reference(s) were left undecided because GitHub did not answer")
    return 0


def utf8_console() -> None:
    """Write the console streams as UTF-8, whatever the host code page is."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("first", nargs="?")
    for flag in ("--repo", "--checkout", "--min-age-hours"):
        pre.add_argument(flag)
    if pre.parse_known_args(args)[0].first == "cleanup":
        sys.modules.setdefault("pr_review", sys.modules[__name__])
        import pr_cleanup

        rest = list(args)
        rest.remove("cleanup")
        return pr_cleanup.main(rest)
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "cmd", choices=["attest", "claims", "cleanup", "comment", "status", "reply", "wait"]
    )
    ap.add_argument("number", type=int, nargs="?")
    # No default, because a default names one repository and every run from elsewhere would read that one.
    ap.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/NAME",
        help="the repository the pull request is in, since a pull "
        "request number identifies no repository on its own",
    )
    ap.add_argument("--timeout", type=int, default=2700, help="seconds (default 45m)")
    ap.add_argument(
        "--pickup-grace",
        type=int,
        default=300,
        help="deprecated compatibility option, accepted but ignored",
    )
    ap.add_argument(
        "--check-grace",
        type=int,
        default=CHECK_GRACE,
        help="seconds a check may sit queued, or a required status go unposted, "
        f"before either reads as unstarted (default {CHECK_GRACE // 60}m)",
    )
    ap.add_argument(
        "--check-stall",
        type=int,
        default=CHECK_STALL,
        help="seconds a check may run before its duration is reported "
        f"(default {CHECK_STALL // 60}m)",
    )
    ap.add_argument(
        "--ignore-quota-signal",
        action="store_true",
        help="wait: request and poll the full --timeout even where the reviewer's own most "
        "recent activity elsewhere in this repository is a quota-limit refusal with nothing "
        "answering it since, or this pull request's newest Copilot review is a quota or "
        "error refusal, pass this once the quota is believed to have reset",
    )
    ap.add_argument(
        "--request",
        action="store_true",
        help="wait: request a Copilot round even on a pull request into a branch other than the "
        "default, which an attested local pass otherwise covers",
    )
    ap.add_argument(
        "--min-rounds",
        type=int,
        default=0,
        metavar="N",
        help="wait: treat the head as covered only once the reviewer's total rounds on this "
        "pull request, the same count `status` prints as `rounds=`, exceed N. Pass the "
        "`rounds=` a prior `status` already read, after re-requesting a review on a head that "
        "already carries one: `reviewed_head` alone would report that earlier round as done "
        "at once, never waiting for the fresh one the re-request was for. N is the count "
        "already seen, not the count to reach: on a pull request reading `rounds=3`, N=3 "
        "waits for the fourth round and N=4 for a fifth (default 0, so any round counts, "
        "unchanged from before this flag existed)",
    )
    # `reply` takes the finding's words, and there is no argument an id fits in.
    ap.add_argument(
        "--match",
        metavar="TEXT",
        help="reply: words from the finding, matched against the thread's opening "
        "comment, and required to select exactly one unresolved thread",
    )
    ap.add_argument(
        "--path",
        metavar="FILE",
        help="reply: narrow --match to one file, for a file with several findings",
    )
    ap.add_argument(
        "--all-identical",
        action="store_true",
        help="reply: where --match selects several threads whose opening comments read the "
        "same, a finding posted twice, answer each of them with --body rather than refusing",
    )
    ap.add_argument(
        "--body",
        metavar="TEXT",
        help="comment or reply: the answer to post, carrying the fixing commit SHA or "
        "the evidence that disproves the finding",
    )
    ap.add_argument("--fingerprint", help="comment: acknowledge the unplaced set it names")
    ap.add_argument(
        "--resolve",
        action="store_true",
        help="reply: resolve the thread once the reply is confirmed",
    )
    ap.add_argument(
        "--checkout",
        metavar="DIR",
        help="attest: the checkout holding the pull request's head and its recorded local pass "
        "(default the current directory)",
    )
    a = ap.parse_args(args)
    # Named for the command they belong to, since an ignored flag reads as one that took effect.
    reply_only = {
        "--match": a.match,
        "--path": a.path,
        "--resolve": a.resolve or None,
        "--all-identical": a.all_identical or None,
    }
    if a.cmd != "reply":
        for flag, value in reply_only.items():
            if value is not None:
                ap.error(f"{flag} belongs to `reply`, not `{a.cmd}`")
    if a.cmd != "wait" and a.request:
        ap.error(f"--request belongs to `wait`, not `{a.cmd}`")
    if a.cmd == "cleanup":
        ap.error("`cleanup` takes its arguments after it, as `cleanup NUMBER --repo OWNER/NAME`")
    if a.cmd != "attest" and a.checkout is not None:
        ap.error(f"--checkout belongs to `attest` or `cleanup`, not `{a.cmd}`")
    if a.number is None:
        ap.error("the following arguments are required: number")
    if a.cmd != "comment" and a.fingerprint is not None:
        ap.error(f"--fingerprint belongs to `comment`, not `{a.cmd}`")
    if a.cmd not in ("comment", "reply") and a.body is not None:
        ap.error(f"--body belongs to `comment` or `reply`, not `{a.cmd}`")
    required = ["--body"] + (["--match"] if a.cmd == "reply" else [])
    if a.cmd in ("comment", "reply"):
        values = {"--body": a.body, "--match": a.match}
        for flag in required:
            if not (values[flag] or "").strip():
                ap.error(
                    f"{a.cmd} requires a non-empty {flag}, since an empty answer records nothing"
                )
    if a.pickup_grace < 0:
        ap.error("--pickup-grace cannot be negative")
    if a.min_rounds < 0:
        ap.error("--min-rounds cannot be negative")
    # A negative threshold would report every check in that state on every run.
    for name in ("check_grace", "check_stall"):
        if getattr(a, name) < 0:
            ap.error(f"--{name.replace('_', '-')} cannot be negative")
    # The two thresholds mean opposite things and the readings invert if the order does.
    if a.check_grace >= a.check_stall:
        ap.error(
            "--check-grace must be less than --check-stall, since a queued check is "
            f"judged sooner than a running one (got {a.check_grace} and {a.check_stall})"
        )
    # A bare name is the near-miss a required argument still admits, and unpacking it raises a
    # ValueError traceback rather than saying which half is missing.
    owner, _, repo = a.repo.partition("/")
    if not owner or not repo or "/" in repo:
        ap.error(f"--repo takes OWNER/NAME, not {a.repo!r}")

    if a.cmd == "claims":
        return check_claims(owner, repo, a.number)

    if a.cmd == "attest":
        return attest(owner, repo, a.number, a.checkout or ".")

    if a.cmd == "comment":
        marker = f"\n\n{ACK_MARKER.format(a.fingerprint)}" if a.fingerprint else ""
        return comment_on_pr(owner, repo, a.number, a.body.strip() + marker)

    if a.cmd == "status":
        # One payload renders the digest and decides the code, as in `wait`.
        pr = full_read(owner, repo, a.number)
        out, _ = digest(owner, repo, a.number, pr=pr, grace=a.check_grace, stall=a.check_stall)
        print(out)
        return report_verdict(pr)

    if a.cmd == "reply":
        return reply_to_thread(
            owner, repo, a.number, a.match, a.body, a.path, a.resolve, a.all_identical
        )

    # `wait` mutates through the auto-request, so a cross-owner target is refused before the read as well.
    ok, why = in_scope(owner)
    if not ok:
        print(f"status=OUT_OF_SCOPE nothing was written: {why}")
        return 64

    start = time.monotonic()
    pr = gql(Q_LIVE, owner, repo, a.number)
    rounds_before = len(reviewer_nodes(pr, "reviews"))
    done, answer, drift = liveness(pr, a.min_rounds)
    if done and a.ignore_quota_signal:
        full = gql(Q_FULL, owner, repo, a.number)
        if refusing_review(full) and not reviewed_head(full):
            a.min_rounds = max(a.min_rounds, len(reviewer_nodes(full, "reviews")))
            done = False
    # Read whenever nothing has landed yet, since a pending request with no answer is the shape repo-wide quota exhaustion leaves.
    # The bot id for a fresh request comes from the same traversal.
    history = [] if done or answer or drift else copilot_history(owner, repo)
    snapshot = None if done or drift else gql(Q_FULL, owner, repo, a.number)
    # `--ignore-quota-signal` changes only whether the signal is acted on, not whether the history is read.
    # The history is unfiltered, so a review on an earlier head of this pull request counts as evidence.
    signal = None if a.ignore_quota_signal else quota_signal(history)
    quota_stop = (
        None if not signal or snapshot is None else standing_quota(history, snapshot, a.number)
    )
    stopped = (
        None if a.ignore_quota_signal or answer or snapshot is None else stopping_refusal(snapshot)
    )
    held = (
        snapshot is not None
        and not a.request
        and not reviewer_requested(pr)
        and holds(snapshot)
        and (not answer or attested(snapshot))
    )
    # Request before the first poll, so `wait` makes a review happen rather than only watching for one.
    # Skipped once a round past --min-rounds covers the head, Copilot answered outside a review, or a request is pending.
    recorded: bool | None = None
    final: dict | None = None
    if (
        not done
        and not answer
        and not drift
        and not quota_stop
        and not stopped
        and not held
        and not reviewer_requested(pr)
    ):
        line, recorded = request_copilot_review(
            owner, repo, a.number, pr["id"], copilot_bot_id(history), POLL_DELAYS[0]
        )
        if recorded is False:
            final = gql(Q_FULL, owner, repo, a.number)
            if reviewer_requested(final):
                recorded, final = None, None
            else:
                line += ", and the request recorded nothing on the pull request"
        print(f"auto-request: {line}")
    if recorded is False:
        print(
            "note: the review request returned success and recorded nothing on this pull "
            "request, no pending reviewer and no review-request event, so this wait stops here "
            "rather than polling --timeout out against a request that does not exist."
        )
    elif held and snapshot is not None and not local_cover(snapshot):
        why = (
            "An attestation is published, but a Copilot refusal on this head overrules it."
            if attested(snapshot)
            else "Run attest once a recorded pass covers this head."
        )
        print(
            "note: this pull request merges into a branch other than the default, so its head is "
            "to be covered by an attested local pass rather than a Copilot round, which runs once "
            "per change at the promotion, and this wait requests nothing. No attested local pass "
            f"covers this head yet. {why} Pass --request to ask for a round anyway."
        )
    elif held:
        print(
            "note: this pull request merges into a branch other than the default, so its head is "
            "covered by an attested local pass rather than a Copilot round, which runs once per "
            "change at the promotion, and this wait requests nothing. Pass --request to ask "
            "for a round anyway. An attested head has its checks polled instead, while they can "
            "still move its merge."
        )

        def still_held(polled: dict) -> bool:
            return (
                holds(polled)
                and local_cover(polled)
                and held_checks_open(
                    polled,
                    check_nodes(polled),
                    datetime.now(UTC),
                    a.check_grace,
                    a.check_stall,
                    required_for(polled, owner, repo),
                )
            )

        assert snapshot is not None
        last_full = [snapshot]

        def held_read() -> dict:
            polled = gql(Q_HELD, owner, repo, a.number)
            if still_held(polled):
                return polled
            last_full[0] = gql(Q_FULL, owner, repo, a.number)
            return last_full[0]

        polled = backoff(last_full[0], held_read, still_held, start, a.timeout)
        final = polled if polled is last_full[0] else None
    elif stopped and not reviewer_requested(pr):
        print(
            "note: this pull request's newest Copilot review is a refusal naming the account "
            "quota or an error, which is what the weekly rate limit posts, so this wait requests "
            "nothing and stops here. Pass --ignore-quota-signal to request and poll anyway, "
            "once the limit is believed to have reset."
        )
    elif quota_stop and not reviewer_requested(pr):
        # The poll is skipped rather than shortened, since the account quota is not answered by this pull request's silence.
        number, hist_refusal = quota_stop
        print(
            f"note: the reviewer's own most recent activity anywhere in this repository, on "
            f"pull request #{number} at {hist_refusal.get('_at') or 'an unknown time'}, is a "
            "quota-limit refusal with nothing answering it since, so this wait stops here "
            "rather than polling --timeout out against the same account state. Pass "
            "--ignore-quota-signal to poll anyway, once the quota is believed to have reset."
        )
    else:
        backoff(
            (done, answer, drift),
            lambda: live_state(owner, repo, a.number, a.min_rounds),
            lambda s: not any(s),
            start,
            a.timeout,
        )

    # One payload decides the digest and the exit code together, and the digest also prints at the timeout.
    final = full_read(owner, repo, a.number, final)
    now = datetime.now(UTC)
    # Parsed here and handed down so the digest and the exit code share one read of the rollup.
    checks = check_nodes(final)
    stuck = checks_stuck(checks, now, a.check_grace, a.check_stall)
    out, _ = digest(
        owner,
        repo,
        a.number,
        pr=final,
        now=now,
        grace=a.check_grace,
        stall=a.check_stall,
        checks=checks,
    )
    print(out)
    print(f"waited={int(time.monotonic() - start)}s")
    # A stuck required check decides the code only once the review itself is sound.
    held = held and holds(final)
    covered = held and local_cover(final)
    halted = None if a.ignore_quota_signal else stopping_refusal(final)
    if covered or (not halted and head_review_done(final, a.min_rounds)):
        verdict = report_verdict(final)
        if verdict == 50 and checks_settling(checks, now, a.check_grace, a.check_stall):
            print(
                "note: a check on this head has not concluded, and this wait returns 50 at once "
                "while the hold above stands, which for a headline is until the head moves. Once "
                "it is answered, watch the checks with a bounded "
                f"`timeout 1800 gh pr checks {a.number} --repo {owner}/{repo} --watch`, then run "
                "status"
            )
        if verdict:
            return verdict
        runs = runs_open(final)
        if covered and held_checks_open(
            final,
            checks,
            now,
            a.check_grace,
            a.check_stall,
            required_for(final, owner, repo),
        ):
            print(
                "status=CHECKS_PENDING an attested local pass covers this head, and by the "
                "timeout a check had not concluded, none had posted, a workflow run on the head "
                "had not completed, a check the base branch requires had not registered or came "
                "from another app, or the merge was still UNKNOWN, so the merge is not ready yet: "
                "wait again, or read the checks above"
                + _unmet_note(final, required_for(final, owner, repo), checks)
                + (f" (not completed: {', '.join(runs)})" if runs else "")
            )
            return 30
        # The rollup carries checks no ruleset requires, and CLEAN proves no gate is outstanding.
        gates = [n.get("name") or "unnamed" for n, _ in stuck if n.get("required")]
        if final.get("mergeStateStatus") == "BLOCKED" and gates:
            print(
                "status=CHECKS_NOT_MERGEABLE the review loop is closed, the merge reads "
                "BLOCKED, and a required check is in a shape waiting does not clear, "
                f"required stuck {', '.join(repr(g) for g in gates)}: read their lines in the "
                "block above, since a starved check wants a re-run, an unposted one its poster, "
                "a gated one its approval or its timer, a long one a judgment, and a failed one a fix. It need "
                "not be all that blocks "
                "the merge, because BLOCKED is also worn by a thread or a missing approval"
            )
            return 44
        if covered and (
            unrun := checks_never_ran(
                final, checks, required_for(final, owner, repo, frozenset({"DIRTY", "BEHIND"}))
            )
        ):
            print(
                "status=CHECKS_MISSING an attested local pass covers this head and nothing holds "
                f"the poll, but {unrun}. The merge reads {final.get('mergeStateStatus')}, so it is "
                "not ready to merge"
            )
            return 45
        return 0
    # Every refusal and quota state is 41, the kind named by its status line.
    refusal = refusing_review(final)
    if refusal is None and not a.ignore_quota_signal:
        refusal = stopping_refusal(final)
    if refusal and quota_refusal(refusal):
        print(
            "status=COPILOT_QUOTA_EXHAUSTED the newest Copilot review declined because the "
            "requesting account has reached its Copilot review quota, printed above under "
            "COPILOT REFUSED THIS ROUND: that is an account-level state, not one this pull "
            "request or a re-request clears, so proceed on the coverage the other reviewers "
            "already gave this pull request rather than waiting on Copilot again"
        )
        return 41
    if refusal and (limit := confirmed_quota(owner, repo, refusal)):
        print(
            "status=COPILOT_QUOTA_EXHAUSTED the newest Copilot review on this pull request says "
            "it encountered an error, and the reviewer's own run log names the cause: "
            f"{limit}. Do not re-request until the limit resets, then pass "
            "--ignore-quota-signal, and proceed meanwhile on the coverage the other reviewers "
            "already gave"
        )
        return 41
    if refusal and possible_quota(refusal):
        print(
            "status=COPILOT_ERROR_POSSIBLE_QUOTA the newest Copilot review on this pull request "
            "says it encountered an error and did not review, printed above under COPILOT "
            "REFUSED THIS ROUND, which is the body the weekly rate limit posts. Read it as a "
            "possible quota hit: do not re-request, proceed on the coverage the other reviewers "
            "already gave, and hand the state to the maintainer, who can pass "
            "--ignore-quota-signal once the limit is believed to have reset"
        )
        return 41
    if held and not refusal:
        print(
            "status=AWAITING_LOCAL_PASS no Copilot round covers this head and none was requested, "
            "since a head into a branch other than the default is covered by a local pass. Run "
            "the local strict review, record it, push, and run `pr_review.py attest` from the "
            "checkout, or pass --request to ask Copilot for a round instead"
        )
        return 49
    if refusal:
        print(
            "status=REVIEW_IS_A_REFUSAL the review carrying the head says it did not review, "
            "so it covers nothing and no further review follows it: read the body above, "
            "since a file-count refusal is cleared by splitting the pull request, and "
            "re-requesting this head clears nothing"
        )
        return 41
    if answered_outside_review(final):
        print(
            "status=ANSWERED_OUTSIDE_REVIEW no Copilot review covers this head, and the reviewer "
            "answered without reviewing, so read the comment above and decide, since where it "
            "declines or names a limit no review follows and re-requesting does not clear it"
        )
        return 40
    if recorded is False:
        print(
            "status=REQUEST_NOT_RECORDED no Copilot review covers this head, and the review "
            "request returned success and added neither a pending reviewer nor a review-request "
            "event. The likely cause is an exhausted Copilot allowance on the requesting account, "
            "which is the maintainer's to restore, so hand it to the maintainer"
        )
        return 40
    if reviewer_login_drift(final):
        print(
            "status=REVIEWER_LOGIN_DRIFT no Copilot review covers this head under the login every "
            "query filters on, and the digest notes the login the reviewer now writes as. The "
            f"remedy is a change to the login this script reads, on {HUB}"
        )
        return 40
    # Ranked last, being inferred from elsewhere in the repository rather than read here.
    if signal and (standing := standing_quota(history, final, a.number)):
        number, hist_refusal = standing
        print(
            "status=COPILOT_QUOTA_EXHAUSTED_REPO_WIDE this pull request's head carries no "
            f"Copilot review or comment of its own, and the reviewer's own most recent activity "
            f"in this repository, on pull request #{number} at "
            f"{hist_refusal.get('_at') or 'an unknown time'}, was a refusal citing the "
            "account quota limit rather than a review of this head. Proceed on the coverage "
            "the other reviewers already gave this pull request, or pass --ignore-quota-signal "
            "to poll this pull request's own head for the full --timeout"
        )
        return 41
    if head_review_done(final, rounds_before):
        rounds = len(reviewer_nodes(final, "reviews"))
        print(
            f"status=PENDING this pull request now has {rounds} rounds, up from "
            f"{rounds_before} when this wait began, and one that landed covers this head, but "
            f"--min-rounds {a.min_rounds} waits for round {a.min_rounds + 1} or later and none "
            "past that position covers the head. N is the `rounds=` a `status` read before "
            "the re-request, so one above it waits for the round after the one the re-request "
            "was for"
        )
        return 30
    print("status=PENDING")
    return 30


if __name__ == "__main__":
    sys.exit(main())
