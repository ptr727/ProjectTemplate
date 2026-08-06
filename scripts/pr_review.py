#!/usr/bin/env python3
"""Consolidated Copilot-review status for a PR - one command, one compact digest.

Why this exists: 4,513 of the measured `gh` invocations were one call per agent turn, and
each turn re-bills the whole session context. The bytes `gh` returns are trivial, averaging
574, so the round-trips are the entire cost. This collapses a poll cycle into a single
invocation whose output is a few hundred bytes. See GOVERNANCE.md "Context and Delegation
Discipline" for the rule this implements.

Subcommands
  status   One digest line, any unresolved threads, and any suppressed findings. Read-only.
  wait     Poll until Copilot's review lands on the current head, then print the digest.
           The loop runs in-process, so a 45-minute wait costs one agent turn, not 90.
           Exit 0 = review present, 30 = still pending at timeout (pending is not failure),
           40 = Copilot answered outside a formal review, so read the printed body.
           40 reports the shape of that answer and reads nothing of its cause: an answer
           carrying no commit covers no head, so the wait ends and the reader decides.
           41 = the review carrying the head says it did not review, so it covers nothing.
           42 = the review loop closed but a required check is in a shape no wait clears:
           queued with nothing acting on it, running far past what the job costs, or
           failed. A check merely still running normally is not this, and exits 0.
           50 = the request is pending and nothing picked it up, which no amount of
           waiting changes. Recovery is two mutations, and they stay in the runbook.

Read-only by design. Mutations (re-request review, reply, resolve thread) are
deliberately NOT implemented here - they are state-changing calls that must stay
visible to the gh-write-guard PreToolUse hook and to review. See
.github/copilot-instructions.md for the mutation runbook.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, time
from datetime import datetime, timezone

REVIEWER = 'copilot-pull-request-reviewer'

# A check that has not started, in every spelling the two rollup enums carry between them.
# QUEUED is the one a starved job wears, and WAITING, PENDING and REQUESTED cover a gate.
# Those three are a CheckRun's, where PENDING means dispatched and not begun.
# EXPECTED is a StatusContext's, meaning a required status nothing has posted yet.
# A StatusContext's own PENDING is the opposite and is translated away in check_nodes.
# Reading QUEUED alone scores every one of these as running, and EXPECTED as a failure.
NOT_STARTED = frozenset({'QUEUED', 'WAITING', 'PENDING', 'REQUESTED', 'EXPECTED'})
# What counts as a check having passed, rather than as one still deciding or failed.
# SKIPPED and NEUTRAL are passes, since the fleet aggregator pattern skips the conditional jobs.
# Treating a skip as unfinished reports four blockers on every green pull request here.
CHECK_OK = frozenset({'SUCCESS', 'SKIPPED', 'NEUTRAL'})
# How long a queued check is simply a check starting.
# Observed pickup on a healthy run here is one to two minutes.
# This is the pickup grace's five minutes for the same reason that one is.
CHECK_GRACE = 300
# How long a running check runs before its duration is worth reporting.
# Generous on purpose, since this repository's lint job legitimately takes eleven minutes.
# A fleet repository building and testing .NET takes longer still.
# A threshold that flags those trains the reader to skim the genuinely hung job too.
CHECK_STALL = 1800

# A review body can carry a collapsed block of findings withheld from the inline threads.
# Those appear nowhere in `reviewThreads`, so polling threads alone reports a clean pass.
# The alternation is the runbook's, since the heading wording has changed once already.
# Matching one phrasing alone reports zero on a review that has them.
SUPPRESSED = re.compile(r'Suppressed comments|low confidence', re.IGNORECASE)
# A refusal declines the round as a formal review carrying the head and no threads.
# That is the clean pass byte for byte, so every coverage check passes over a round that never ran.
# The alternation is the runbook's for the same reason the one above is.
# One phrasing is one rewording away from reading a refusal as a review.
# The dot covers the apostrophe in the typographic spelling and the ASCII one alike.
# It also keeps the published filter usable inside single quotes, which neither survives.
REFUSAL = re.compile(r'wasn.t able to review|was not able to review|unable to review',
                     re.IGNORECASE)
DETAILS = re.compile(r'<details>(.*?)</details>', re.DOTALL | re.IGNORECASE)
SUMMARY = re.compile(r'<summary>(.*?)</summary>', re.DOTALL | re.IGNORECASE)
TAGS = re.compile(r'</?(?:details|summary)>', re.IGNORECASE)
COUNT = re.compile(r'\((\d+)\)')
# What makes a line a heading rather than prose mentioning the phrase, in either markup.
# A line that is neither still qualifies where it carries a count.
# The section has appeared as a bare line too, and a count is what prose does not carry.
HEADING = re.compile(r'\s*(?:#{1,6}\s|<summary)', re.IGNORECASE)

# How many of the newest reviews and comments both queries read.
# A narrow window drops the reviewer's answer behind ordinary discussion, reporting no answer.
# A test holds this equal to the number the queries carry, since a drift between them reads clean.
WINDOW = 100

# The timeline spells the reviewer a third way, as login `Copilot` with type `Bot`.
# GraphQL says `copilot-pull-request-reviewer`, and REST user objects add a `[bot]` suffix.
# The predicate is the type plus a loose login match rather than any one spelling.
# Requests are the reviewer's own, since a human requested later is a different request.
# Reading one as the newest reports a picked-up review as never picked up.
TIMELINE_JQ = (
    '.[] | select(.event == "copilot_work_started" or (.event == "review_requested"'
    ' and .requested_reviewer.type == "Bot"'
    ' and ((.requested_reviewer.login // "") | ascii_downcase | test("copilot"))))'
    ' | "\\(.event) \\(.created_at)"'
)

# Liveness query: timestamps and ids only, no comment or review bodies.
# A liveness check does not need the finding text, and re-fetching bodies was 76% of polls.
# It does need the reviewer's non-review answers.
# A wait reading formal reviews alone treats a refusal as an unmet condition.
Q_LIVE = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    headRefOid
    reviews(last:100){ nodes{ author{login} state commit{oid} submittedAt } pageInfo{ hasPreviousPage } }
    comments(last:100){ nodes{ author{login} createdAt } pageInfo{ hasPreviousPage } }
    reviewRequests(first:10){ nodes{ requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } }
  }}}
"""

# Full query: run once on transition, not per poll.
# The rollup rides this query rather than a REST call, so reading the checks costs no round-trip.
# It is asked of the last commit because a rollup hangs off a commit object.
# A case holds that commit equal to `headRefOid`, since a rollup a push ago still renders whole.
Q_FULL = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    headRefOid mergeable mergeStateStatus
    reviews(last:100){ nodes{ author{login} state commit{oid} submittedAt body } pageInfo{ hasPreviousPage } }
    reviewThreads(first:100){ nodes{ id isResolved
      comments(first:1){ nodes{ author{login} path line body } } }}
    comments(last:100){ nodes{ author{login} createdAt body } pageInfo{ hasPreviousPage } }
    reviewRequests(first:10){ nodes{ requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } }
    commits(last:1){ nodes{ commit{ oid statusCheckRollup{ state
      contexts(first:100){ nodes{
        __typename
        ... on CheckRun{ name status conclusion startedAt }
        ... on StatusContext{ context state createdAt }
      }}}}}}
  }}}
"""


def gql(query: str, owner: str, repo: str, num: int) -> dict:
    r = subprocess.run(
        ['gh', 'api', 'graphql', '-f', f'query={query}',
         '-F', f'o={owner}', '-F', f'r={repo}', '-F', f'n={num}'],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[:800])
        raise SystemExit(f'gh graphql failed rc={r.returncode}')
    return json.loads(r.stdout)['data']['repository']['pullRequest']


def timeline(owner: str, repo: str, num: int) -> list[tuple[str, str]]:
    """The request and pickup events, oldest first, as (event, timestamp).

    GraphQL carries no `copilot_work_started`, so this is the one REST reader here.
    `--jq` projects inside gh rather than after it, since `--paginate` without one emits a
    concatenated array per page that is not valid JSON on every gh a fleet machine may carry.
    `per_page` is the page size the pagination actually costs, and the default of 30 turns a
    long-running pull request into six requests a reading where the maximum makes it two.
    """
    r = subprocess.run(
        ['gh', 'api', '--paginate', f'repos/{owner}/{repo}/issues/{num}/timeline?per_page=100',
         '--jq', TIMELINE_JQ],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[:800])
        raise SystemExit(f'gh timeline failed rc={r.returncode}')
    return [(ln.split(' ', 1)[0], ln.split(' ', 1)[1])
            for ln in r.stdout.splitlines() if ' ' in ln]


def never_picked_up(events: list[tuple[str, str]]) -> str:
    """The newest request's timestamp where no pickup followed it, otherwise the empty string.

    A request the reviewer accepts raises `copilot_work_started` within about half a minute, so
    a request with no pickup after it is not a slow review, it is a request nothing is acting on.
    The two states look identical from the reviews alone, which is how one sat for thirteen hours
    reading as pending. Elapsed time cannot separate them either, since a genuinely slow round
    also produces no review, and only the pickup event says whether anything is working.
    """
    requested = [t for e, t in events if e == 'review_requested']
    started = [t for e, t in events if e == 'copilot_work_started']
    if not requested:
        return ''
    newest = max(requested)
    return '' if any(t >= newest for t in started) else newest


def reviewer_requested(pr: dict) -> bool:
    """True where the reviewer sits in the pending request set.

    Read from GraphQL rather than `gh pr view --json reviewRequests`, which omits a Bot
    reviewer entirely and reports an empty set while the reviewer is sitting in it.
    """
    return any((n.get('requestedReviewer') or {}).get('login') == REVIEWER
               for n in ((pr.get('reviewRequests') or {}).get('nodes') or []))


def reviewer_nodes(pr: dict, field: str) -> list[dict]:
    """The reviewer's own nodes under `field`, oldest first as the API returns them."""
    return [n for n in ((pr.get(field) or {}).get('nodes') or [])
            if (n.get('author') or {}).get('login') == REVIEWER]


def answered_outside_review(pr: dict) -> dict | None:
    """The reviewer's newest plain comment, where it postdates its newest formal review.

    The test is the shape of the answer rather than its cause, which this reads nothing of:
    a comment carries no commit, so it satisfies no coverage check whatever it says.
    Treating that shape as an unmet condition is what leaves a wait with nothing at its end.
    A comment older than the newest review is spent, since the review it preceded did land.
    """
    comments = reviewer_nodes(pr, 'comments')
    # A blind review window leaves no honest baseline to date a comment against.
    # An empty one dates every comment as newer, so each reads as an answer.
    # Reporting nothing keeps the wait polling, where a wrong answer ends it outright.
    if not comments or window_blind(pr, 'reviews'):
        return None
    newest = max(comments, key=lambda n: n.get('createdAt') or '')
    reviews = reviewer_nodes(pr, 'reviews')
    latest_review = max((n.get('submittedAt') or '' for n in reviews), default='')
    return newest if (newest.get('createdAt') or '') > latest_review else None


def window_blind(pr: dict, field: str) -> bool:
    """True where the reviewer's own nodes can sit behind the window, so the view cannot decide.

    Each query reads the newest nodes rather than the reviewer's, so ordinary traffic is what
    pushes theirs out of reach. Nodes arrive in creation order, so anything behind the window is
    older than everything inside it: one of the reviewer's in view bounds every hidden one as
    older still, which settles the question rather than leaving it open. `hasPreviousPage` is
    what says anything is back there at all, since a full window and a window holding the lot
    are the same length.
    """
    older = ((pr.get(field) or {}).get('pageInfo') or {}).get('hasPreviousPage')
    return bool(older) and not reviewer_nodes(pr, field)


def stall_of(owner: str, repo: str, num: int, pr: dict) -> str:
    """The stalled request's timestamp for this payload, or the empty string where none.

    Derived from the payload it is reported beside, since a stall read earlier describes a
    pull request that has since moved: a request picked up after the reading still reports as
    picked up by nothing. A covered head or no pending request settles it without a REST call.
    """
    if reviewed_head(pr) or not reviewer_requested(pr):
        return ''
    return never_picked_up(timeline(owner, repo, num))


def refusal_of(node: dict) -> str:
    """The review's body where its opening line says the reviewer did not review, otherwise empty.

    Read over the opening rather than the whole body, because a refusal replaces the review and
    is the only thing the body carries, where a review that merely quotes the wording carries it
    below its own overview. This script and its documentation are that quotation, so a body-wide
    match would report the pull request adding this check as a refusal, which is the false
    positive the suppressed-block matcher already had once.

    The opening is one line rather than two, and the second line is where the cost of widening
    it shows: a review's first line is its heading and its second is the overview prose, which
    is exactly where a review describing this check states the wording. Reading two lines passed
    every case here except that one, which is the case that matters. A refusal introduced by a
    heading would sit below the opening and be missed, and answering that shape means telling it
    from an overview rather than reading one line further.
    """
    body = node.get('body') or ''
    opening = next((ln for ln in body.splitlines() if ln.strip()), '')
    return body if REFUSAL.search(opening) else ''


def refusing_review(pr: dict) -> dict | None:
    """The reviewer's newest refusal carrying the current head, where one is there.

    Head-scoped, unlike a suppressed finding, because a refusal is a statement about one commit:
    a push retires it, and the round the push raises either reviews that head or refuses it in
    its own right.

    A refusal alongside a genuine review of the same head is spent too, and that is the caller's
    reading rather than this one's, since what spends it is coverage this cannot see from a
    refusal alone. Both callers hold it: `main` returns 0 on `reviewed_head` before reaching
    here, and the digest reports the field only where nothing covers the head.
    """
    head = pr['headRefOid']
    refusals = [n for n in reviewer_nodes(pr, 'reviews')
                if (n.get('commit') or {}).get('oid') == head and refusal_of(n)]
    return max(refusals, key=lambda n: n.get('submittedAt') or '') if refusals else None


def reviewed_head(pr: dict) -> bool:
    """True where one of the reviewer's own reviews covers the current head's commit.

    A refusal is not coverage. It is a formal review, `state: COMMENTED`, carrying the head's
    commit and raising no threads, so it satisfies every check a clean pass does and renders a
    digest identical to one. That is how a pull request of 301 changed files, one over the
    reviewer's limit, sat one command from merging on a review that never ran.

    The liveness query carries no bodies, so a refusal reads there as ordinary coverage. That is
    deliberate rather than a gap: it ends the wait, which is what a terminal outcome should do,
    and the full read every wait finishes with is what tells the two apart. No exit code and no
    merge decision is taken from the liveness reading.
    """
    head = pr['headRefOid']
    return any((n.get('commit') or {}).get('oid') == head and not refusal_of(n)
               for n in reviewer_nodes(pr, 'reviews'))


def age(stamp: str, now: datetime) -> float | None:
    """Seconds from `stamp` to `now`, or None where the stamp is absent or unparseable.

    None is returned rather than zero or a raise, because every caller treats an unknown age as
    "cannot judge this" and reports nothing. Zero would read as a check that just started, which
    is the reading that reports a job stuck for hours as fresh.
    """
    if not stamp:
        return None
    try:
        # `fromisoformat` reads the trailing Z only from 3.11, and a fleet machine may carry less.
        return (now - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
    except ValueError:
        return None


def check_nodes(pr: dict) -> list[dict]:
    """The head commit's checks, each as {name, state, conclusion, started}.

    A rollup carries two node shapes and they spell every field differently: a CheckRun has a
    `name`, a `status` and a `conclusion`, while a StatusContext has a `context` and a single
    `state` that folds both. Reading one shape's keys off the other yields None for all of them,
    which scores an external status as a check in no state at all, so neither pending nor failed.
    They are normalized here so one reading serves both.
    """
    commits = ((pr.get('commits') or {}).get('nodes') or [])
    rollup = ((commits[0].get('commit') or {}) if commits else {}).get('statusCheckRollup') or {}
    out = []
    for n in ((rollup.get('contexts') or {}).get('nodes') or []):
        if n.get('__typename') == 'CheckRun':
            out.append({'name': n.get('name') or '', 'state': n.get('status') or '',
                        'conclusion': n.get('conclusion') or '',
                        'started': n.get('startedAt') or ''})
        else:
            # A StatusContext reports one field for both, so its state doubles as its conclusion.
            # Its PENDING means the posting system reported the run as under way.
            # A CheckRun's PENDING means the opposite, dispatched and not begun.
            # So the same string is two states, and the shape is knowable only here.
            # Left alone, a long external build reports as queued with no runner assigned.
            # That names a cause the run does not have, on a system that did pick it up.
            state = n.get('state') or ''
            out.append({'name': n.get('context') or '',
                        'state': 'IN_PROGRESS' if state == 'PENDING' else state,
                        'conclusion': state, 'started': n.get('createdAt') or ''})
    return out


def check_shape(node: dict, now: datetime, grace: float, stall: float) -> str:
    """How this check is stuck, or the empty string where it is not.

    Three shapes, because `mergeStateStatus` collapses all of them into `BLOCKED` and each wants
    a different response. A run this session spent twenty-five minutes polling `BLOCKED` on a
    pull request whose only unfinished check was a rollup job no runner ever took, and the cause
    came from the maintainer rather than from any reading here.

    NOT_PICKED_UP is the well-founded one: a job GitHub has dispatched but assigned no runner.
    It is read from the queued state rather than from a runner name, which GraphQL does not carry,
    and the state is sufficient because a job held behind a `needs:` dependency does not appear
    in the rollup at all until that dependency finishes, so there is no dependency-blocked queue
    to mistake for a starved one. Observed pickup on a healthy run is one to two minutes, so the
    grace is the pickup grace for the same reason that one is five minutes: inside it, a queued
    job is simply a job starting.

    RUNNING_LONG is deliberately weaker and says so in its wording, because duration alone cannot
    separate a stalled job from a slow one. This repository's own lint job legitimately runs nine
    to eleven minutes while its aggregator is a single shell conditional, and a fleet repository
    building and testing .NET runs longer still. So the threshold is generous, the elapsed time is
    printed for the reader to judge against what the job costs, and nothing here asserts a fault.

    FAILED is any finished check that did not pass, which is a definite answer rather than a stuck
    one, and it is reported here so that a reader is never left deducing a red check from `BLOCKED`.
    """
    state, conclusion = node.get('state') or '', node.get('conclusion') or ''
    elapsed = age(node.get('started') or '', now)
    if state in NOT_STARTED:
        return 'NOT_PICKED_UP' if elapsed is not None and elapsed > grace else ''
    if state == 'IN_PROGRESS':
        return 'RUNNING_LONG' if elapsed is not None and elapsed > stall else ''
    # A finished check carrying no conclusion yet is still settling, so nothing is reported.
    # Reading an absent verdict as a failure invents a red check out of a race in the API.
    # An unrecognized state would reach the line below and produce that same false failure.
    if not conclusion:
        return ''
    # A conclusion this does not recognize is reported rather than passed over.
    # An unknown verdict blocking a merge is exactly what a reader needs told.
    # A new enum member read as a pass is a red check rendering as a green digest.
    return '' if conclusion in CHECK_OK else 'FAILED'


def checks_stuck(pr: dict, now: datetime, grace: float, stall: float) -> list[tuple[dict, str]]:
    """Every check in a stuck shape, as (node, shape), in the order the rollup returns them."""
    return [(n, s) for n in check_nodes(pr) if (s := check_shape(n, now, grace, stall))]


def checks_tally(pr: dict) -> tuple[int, int]:
    """(checks that have passed, checks there are), so a bare count says how far the head is."""
    nodes = check_nodes(pr)
    return sum(1 for n in nodes if (n.get('conclusion') or '') in CHECK_OK), len(nodes)


def live_state(owner: str, repo: str, num: int) -> tuple[str, bool, dict | None]:
    """Return (head_sha, copilot_reviewed_current_head, copilot_answer_outside_a_review)."""
    pr = gql(Q_LIVE, owner, repo, num)
    return pr['headRefOid'], reviewed_head(pr), answered_outside_review(pr)


def heading_of(block: str) -> str:
    """The block's own heading, unwrapped from `<summary>` where it wears one.

    A block starts at its heading, so the heading is the first line whatever markup it carries.
    Reading further would let a finding's own text supply a count the heading never carried, and
    a wrong count reads exactly like a right one.
    """
    head = block.lstrip()
    m = SUMMARY.search(head)
    return m.group(1) if m and head.lower().startswith('<summary') else head.split('\n', 1)[0]


def suppressed_blocks(body: str) -> list[str]:
    """Return the review body's low-confidence sections, each sliced from its own heading.

    The section has worn three shapes so far: its own `<details>` wrapper, a bare heading in the
    body, and a Markdown heading nested inside the `Review details` wrapper. The wrapper is the
    part that keeps moving, so each region is scanned line by line for the heading rather than
    read for a wrapper's summary: a nested heading is not a summary, and stripping the wrappers
    to look for it outside deletes the very region it sits in. That is the pair that reported
    `suppressed=0` over a body carrying `### Suppressed comments (2)`.

    The heading carries the match rather than the body text, since a review whose prose discusses
    suppressed findings is not itself carrying any. A region ends the block, so a section is not
    read on into the file table that follows it.
    """
    if not body:
        return []
    # Each wrapper's contents, plus what is left outside them all, so a heading is found anywhere.
    # The regions do not overlap, since what the sub deletes is exactly what the findall keeps.
    regions = DETAILS.findall(body) + [DETAILS.sub('', body)]
    blocks = []
    for region in regions:
        lines = region.splitlines()
        for i, line in enumerate(lines):
            if SUPPRESSED.search(line) and (HEADING.match(line) or COUNT.search(line)):
                blocks.append('\n'.join(lines[i:]))
                break
    return blocks


def finding_count(block: str) -> int:
    """The heading's `(N)`, floored at one, since a block reported as zero reads as a clean pass."""
    m = COUNT.search(heading_of(block))
    return max(int(m.group(1)), 1) if m else 1


def digest(owner: str, repo: str, num: int, seen: set[str] | None = None,
           pr: dict | None = None, stalled: str | None = None, now: datetime | None = None,
           grace: float = CHECK_GRACE, stall: float = CHECK_STALL) -> tuple[str, int]:
    """Render the digest, from a caller's payload and stall reading where those are given.

    The caller passes its own readings when the exit code has to agree with what was printed,
    since a review landing between two reads makes a fresh fetch describe a different pull
    request than the one the code was decided from. Passing the stall also spends one REST
    call between the caller and the digest rather than one each. `now` is a parameter for the
    same reason, so a case can hold a check at a known age rather than at whatever the clock
    says when the suite runs.
    """
    pr = gql(Q_FULL, owner, repo, num) if pr is None else pr
    stalled = stall_of(owner, repo, num, pr) if stalled is None else stalled
    now = datetime.now(timezone.utc) if now is None else now
    head = pr['headRefOid']
    revs = reviewer_nodes(pr, 'reviews')
    # A refusal carries the head and covers nothing, so it counts as a round and not as coverage.
    # Reading it as coverage prints `review_on_head=yes` over a review that says it did not run.
    on_head = [n for n in revs
               if (n.get('commit') or {}).get('oid') == head and not refusal_of(n)]
    threads = pr['reviewThreads']['nodes']
    # A deleted account leaves `author` present and null, which `.get('author', {})` returns as
    # None rather than as the default, so the chained lookup crashes the whole digest.
    unresolved = [t for t in threads
                  if not t['isResolved']
                  and ((((t.get('comments') or {}).get('nodes') or [{}])[0]
                        .get('author') or {}).get('login') == REVIEWER)]

    # Every round, not just the head, because a suppressed finding has no resolved state to read.
    # Head-scoping treated "superseded by a push" as "answered", and the two are not the same.
    # A finding nobody replied to left the digest the moment the branch moved, reporting zero.
    # That is how four rounds went unanswered across three pull requests in one day.
    # The head is still marked per block, since a finding on an older round may be moot.
    # Deciding that is the reader's call rather than one the count makes for them.
    blocks = [(n, b) for n in revs for b in suppressed_blocks(n.get('body') or '')]
    on_head_blocks = [b for n, b in blocks if (n.get('commit') or {}).get('oid') == head]
    stale = sum(finding_count(b) for n, b in blocks) - sum(
        finding_count(b) for b in on_head_blocks)

    answer = answered_outside_review(pr)
    # Spent where coverage of the same head landed, the precedence the exit codes already hold.
    # Reported regardless, it prints `review_on_head=yes refusal=YES` over a reviewed head.
    # That tells a reader to split a pull request the reviewer has just reviewed.
    refusal = None if on_head else refusing_review(pr)
    blind = [f for f in ('reviews', 'comments') if window_blind(pr, f)]
    answered = 'yes' if answer else ('unknown' if blind else 'no')
    ok, total = checks_tally(pr)
    stuck = checks_stuck(pr, now, grace, stall)
    lines = [
        # The repository leads the line, since a number alone reads as correct anywhere.
        # A digest of the wrong pull request is well-formed, so naming it is what shows the miss.
        f'repo={owner}/{repo} pr={num} head={head[:8]} rounds={len(revs)} '
        f'review_on_head={"yes" if on_head else "NO"} '
        # A field of its own, since `rounds=1 review_on_head=NO` is also what a stale round is.
        # The two want opposite responses, one a re-request and the other a split pull request.
        # Upper-case for the reason `NO` is, as a state that blocks a merge is not one to skim.
        f'refusal={"YES" if refusal else "no"} '
        f'threads={len(threads)} unresolved={len(unresolved)} '
        f'suppressed={sum(finding_count(b) for n, b in blocks)} '
        f'(on_head={sum(finding_count(b) for b in on_head_blocks)} earlier={stale}) '
        f'answered_outside_review={answered} '
        f'requested={"yes" if reviewer_requested(pr) else "no"} '
        f'merge={pr.get("mergeStateStatus")} '
        # `merge=BLOCKED` names no cause and is worn by every gate alike.
        # A red check, a check nothing runs, an open thread, and a missing approval all read it.
        # One run here polled that word for twenty-five minutes without learning which it was.
        # The tally says how far the head got, and `checks=0/0` says the rollup carried nothing.
        f'checks={ok}/{total}'
        # Named only where there is one to name.
        # A field reading `none` on every green run is one a reader skips when it finally says more.
        + (f' stuck={",".join(sorted({s for _, s in stuck}))}' if stuck else '')
    ]
    if refusal:
        # Printed whole for the reason the comment below is, as the wording carries the remedy.
        # A file-count refusal is cleared by splitting the pull request and a quota one by waiting.
        # This reads neither cause, only that the round declined.
        lines.append('  COPILOT REFUSED THIS ROUND: the review carrying the head says it did '
                     'not review, so it covers nothing and re-requesting the same head repeats '
                     'it, and the body below is what says which remedy applies')
        lines += [f'    {ln.rstrip()}' for ln in (refusal.get('body') or '').splitlines()
                  if ln.strip()]
    for node, shape in stuck:
        # Each shape carries its own remedy, which is the whole point of telling them apart.
        # A reader handed one word for all three retries the wrong thing, or waits on a queue.
        elapsed = age(node.get('started') or '', now)
        mins = 'age unknown' if elapsed is None else f'{int(elapsed // 60)}m'
        if shape == 'NOT_PICKED_UP':
            lines.append(f'  CHECK NOT PICKED UP ({node["name"]!r}, queued {mins} with no '
                         'runner assigned): nothing here starts it, because the runner pool is '
                         'GitHub-hosted, so re-run the workflow or wait on that capacity')
        elif shape == 'RUNNING_LONG':
            lines.append(f'  CHECK RUNNING LONG ({node["name"]!r}, running {mins}): it has a '
                         'runner, so it is not starved, and whether this is hung or merely slow '
                         'is a judgment against what this job normally costs')
        else:
            lines.append(f'  CHECK FAILED ({node["name"]!r}, {node["state"]}/'
                         f'{node["conclusion"]}): a verdict rather than a stuck check, so read '
                         'the run and fix it, since no wait and no re-run clears a real failure')
    if stalled:
        lines.append(f'  REQUEST NOT PICKED UP (requested {stalled}, no copilot_work_started '
                     'since): clear the request and re-request, per the runbook')
    if blind:
        lines.append(f'  BEHIND THE WINDOW ({" and ".join(blind)}): the newest {WINDOW} carry '
                     'none from the reviewer and older ones exist, so this cannot decide')
    if answer:
        # Printed whole for the same reason a suppressed finding is, since it reaches no thread.
        # Its wording is the only thing separating a refusal from an ordinary remark.
        lines.append(f'  COPILOT COMMENT ({answer.get("createdAt")}, newer than any review): '
                     'the reviewer answered without reviewing, so read the body below and '
                     'decide, since a refusal is terminal and a remark is not')
        lines += [f'    {ln.rstrip()}' for ln in (answer.get('body') or '').splitlines()
                  if ln.strip()]
    new = 0
    for t in unresolved:
        c = (t.get('comments') or {}).get('nodes', [{}])[0]
        tid = t['id']
        mark = ''
        if seen is not None:
            if tid in seen:
                continue
            seen.add(tid)
            mark = 'NEW '
            new += 1
        body = ' '.join((c.get('body') or '').split())
        lines.append(f'  {mark}{tid} {c.get("path")}:{c.get("line")} {body[:160]}')
    for n, b in blocks:
        # Printed whole where a thread body is truncated, since a thread can be re-read at its id.
        # A suppressed finding has none, so this digest is the only place it appears.
        # GraphQL returns a null commit for a pending or partial review.
        # An empty sha rendered as "raised on , earlier round", losing what traces the finding.
        sha = ((n.get('commit') or {}).get('oid') or '')[:8]
        if not sha:
            where = 'commit unknown, treat as outstanding'
        elif sha == head[:8]:
            where = 'on head'
        else:
            where = f'raised on {sha}, earlier round'
        lines.append(f'  SUPPRESSED ({where}): no thread to resolve, '
                     'answer it in the PR conversation quoting the finding')
        # Indentation is kept, since a block carries fenced code a flattened line would garble.
        lines += [f'    {ln.rstrip()}' for ln in TAGS.sub('', b).splitlines() if ln.strip()]
    if seen is not None:
        lines[0] += f' new={new}'
    return '\n'.join(lines), len(unresolved)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['status', 'wait'])
    ap.add_argument('number', type=int)
    # No default, because the wrong repository is the failure this argument has actually had.
    # A default names one repository, and every run from elsewhere silently reads that one.
    # The number resolves there, the digest renders, and nothing in the output disagrees.
    # Two runs read a pull request here while one in their own repository was the subject.
    ap.add_argument('--repo', required=True, metavar='OWNER/NAME',
                    help='the repository the pull request is in, since a pull '
                         'request number identifies no repository on its own')
    ap.add_argument('--timeout', type=int, default=2700, help='seconds (default 45m)')
    ap.add_argument('--pickup-grace', type=int, default=300,
                    help='seconds before the first pickup read, and between reads (default 5m)')
    ap.add_argument('--check-grace', type=int, default=CHECK_GRACE,
                    help='seconds a check may sit queued before it reads as unstarted '
                         f'(default {CHECK_GRACE // 60}m)')
    ap.add_argument('--check-stall', type=int, default=CHECK_STALL,
                    help='seconds a check may run before its duration is reported '
                         f'(default {CHECK_STALL // 60}m)')
    a = ap.parse_args(argv)
    # A negative grace leaves the next reading permanently behind the clock.
    # That is the per-poll REST pattern the interval exists to prevent.
    if a.pickup_grace < 0:
        ap.error('--pickup-grace cannot be negative')
    # A negative threshold reports every check in that state, on every run, from the first read.
    # A field that fires always is one a reader learns to skip, which costs the real case.
    for name in ('check_grace', 'check_stall'):
        if getattr(a, name) < 0:
            ap.error(f'--{name.replace("_", "-")} cannot be negative')
    # A bare name is the near-miss a required argument still admits, and unpacking it raises a
    # ValueError traceback rather than saying which half is missing.
    owner, _, repo = a.repo.partition('/')
    if not owner or not repo or '/' in repo:
        ap.error(f'--repo takes OWNER/NAME, not {a.repo!r}')

    if a.cmd == 'status':
        out, _ = digest(owner, repo, a.number, grace=a.check_grace, stall=a.check_stall)
        print(out)
        return 0

    # In-process backoff, so the whole wait costs one agent turn.
    delays = [15, 20, 30, 45, 60, 120]
    start = time.monotonic()
    pr = gql(Q_LIVE, owner, repo, a.number)
    done, answer = reviewed_head(pr), answered_outside_review(pr)
    stalled = ''
    i = 0
    next_pickup = a.pickup_grace
    while not done and not answer:
        elapsed = time.monotonic() - start
        # Read the pickup before the clock, so a request nothing acted on reports as itself.
        # Running the clock out instead would report it exactly as a slow reviewer.
        # The read costs a second call over REST, so it runs on its own interval, not per poll.
        # One reading settles the current request, and the next covers a request a push raises.
        if elapsed > next_pickup and reviewer_requested(pr):
            next_pickup = elapsed + a.pickup_grace
            stalled = never_picked_up(timeline(owner, repo, a.number))
            if stalled:
                break
        if elapsed > a.timeout:
            break
        time.sleep(delays[min(i, len(delays) - 1)])
        i += 1
        # Re-read head each iteration: a push during the wait moves it.
        pr = gql(Q_LIVE, owner, repo, a.number)
        done, answer = reviewed_head(pr), answered_outside_review(pr)

    # One payload decides the digest and the exit code together.
    # Read separately, a review landing between them prints coverage and returns a stalled code.
    # A reader resolves that by believing the code, dropping the review it was just shown.
    # The digest also earns its call at the timeout.
    # A bare PENDING line reports a broken wait and a slow reviewer identically.
    final = gql(Q_FULL, owner, repo, a.number)
    # The stall is re-read here rather than carried out of the loop.
    # A request picked up since that reading would still report as picked up by nothing.
    stalled = stall_of(owner, repo, a.number, final)
    now = datetime.now(timezone.utc)
    out, _ = digest(owner, repo, a.number, pr=final, stalled=stalled, now=now,
                    grace=a.check_grace, stall=a.check_stall)
    print(out)
    print(f'waited={int(time.monotonic()-start)}s')
    if reviewed_head(final):
        # The review loop closing is not the merge gate, and 0 alone was saying it was.
        # A wait ends the moment coverage lands, which leaves the checks mid-flight nearly always.
        # So a merely pending check is not this code, or the code would be the usual outcome.
        # Only a shape no waiting clears earns it, which is what the stuck field already prints.
        # It is read from the same payload the digest was, so the two can never disagree.
        # A rollup carries checks the ruleset does not require, four of six on a green run here.
        # So `BLOCKED` is required of the code as well, borrowing GitHub's own reading.
        # That is cheaper than reading the ruleset's contexts over another call.
        # Without it, a stuck check nothing requires returns 42 on a mergeable pull request.
        # `CLEAN` proves no required gate is outstanding, whatever else the rollup is doing.
        # The digest reports the check either way, so the narrower code costs the reader nothing.
        stuck = checks_stuck(final, now, a.check_grace, a.check_stall)
        if stuck and final.get('mergeStateStatus') == 'BLOCKED':
            print('status=CHECKS_NOT_MERGEABLE the review loop is closed, and a required check '
                  'is in a shape waiting does not clear: read the block above, since a starved '
                  'check wants a re-run, a long one a judgment, and a failed one a fix')
            return 42
        return 0
    # A refusal before an answer, since it names the round that declined where 40 names none.
    # The digest prints both bodies regardless, so the narrower code costs the reader nothing.
    if refusing_review(final):
        print('status=REVIEW_IS_A_REFUSAL the review carrying the head says it did not review, '
              'so it covers nothing and no further review follows it: read the body above, '
              'since a file-count refusal is cleared by splitting the pull request and a quota '
              'one by waiting, and re-requesting this head clears neither')
        return 41
    # An answer before a stall, because the reviewer saying something outranks it saying nothing.
    if answered_outside_review(final):
        print('status=ANSWERED_OUTSIDE_REVIEW the reviewer answered without reviewing, '
              'so read the comment above and decide, since where it declines or names a limit '
              'no review follows and re-requesting does not clear it')
        return 40
    if stalled:
        print(f'status=REQUEST_NOT_PICKED_UP requested {stalled} and no copilot_work_started '
              'followed it, so nothing is working on this and waiting on will not start it: '
              'clear the request and re-request, per the runbook')
        return 50
    print('status=PENDING')
    return 30


if __name__ == '__main__':
    sys.exit(main())
