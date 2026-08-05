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
           50 = the request is pending and nothing picked it up, which no amount of
           waiting changes. Recovery is two mutations, and they stay in the runbook.

Read-only by design. Mutations (re-request review, reply, resolve thread) are
deliberately NOT implemented here - they are state-changing calls that must stay
visible to the gh-write-guard PreToolUse hook and to review. See
.github/copilot-instructions.md for the mutation runbook.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, time

REVIEWER = 'copilot-pull-request-reviewer'

# A review body can carry a collapsed block of findings withheld from the inline threads.
# Those appear nowhere in `reviewThreads`, so polling threads alone reports a clean pass.
# The alternation is the runbook's, since the heading wording has changed once already.
# Matching one phrasing alone reports zero on a review that has them.
SUPPRESSED = re.compile(r'Suppressed comments|low confidence', re.IGNORECASE)
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
Q_FULL = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    headRefOid mergeable mergeStateStatus
    reviews(last:100){ nodes{ author{login} state commit{oid} submittedAt body } pageInfo{ hasPreviousPage } }
    reviewThreads(first:100){ nodes{ id isResolved
      comments(first:1){ nodes{ author{login} path line body } } }}
    comments(last:100){ nodes{ author{login} createdAt body } pageInfo{ hasPreviousPage } }
    reviewRequests(first:10){ nodes{ requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } }
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


def reviewed_head(pr: dict) -> bool:
    """True where one of the reviewer's own reviews carries the current head's commit."""
    head = pr['headRefOid']
    return any((n.get('commit') or {}).get('oid') == head
               for n in reviewer_nodes(pr, 'reviews'))


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
    body, and a markdown heading nested inside the `Review details` wrapper. The wrapper is the
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
           pr: dict | None = None, stalled: str | None = None) -> tuple[str, int]:
    """Render the digest, from a caller's payload and stall reading where those are given.

    The caller passes its own readings when the exit code has to agree with what was printed,
    since a review landing between two reads makes a fresh fetch describe a different pull
    request than the one the code was decided from. Passing the stall also spends one REST
    call between the caller and the digest rather than one each.
    """
    pr = gql(Q_FULL, owner, repo, num) if pr is None else pr
    stalled = stall_of(owner, repo, num, pr) if stalled is None else stalled
    head = pr['headRefOid']
    revs = reviewer_nodes(pr, 'reviews')
    on_head = [n for n in revs if (n.get('commit') or {}).get('oid') == head]
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
    blind = [f for f in ('reviews', 'comments') if window_blind(pr, f)]
    answered = 'yes' if answer else ('unknown' if blind else 'no')
    lines = [
        f'pr={num} head={head[:8]} rounds={len(revs)} '
        f'review_on_head={"yes" if on_head else "NO"} '
        f'threads={len(threads)} unresolved={len(unresolved)} '
        f'suppressed={sum(finding_count(b) for n, b in blocks)} '
        f'(on_head={sum(finding_count(b) for b in on_head_blocks)} earlier={stale}) '
        f'answered_outside_review={answered} '
        f'requested={"yes" if reviewer_requested(pr) else "no"} '
        f'merge={pr.get("mergeStateStatus")}'
    ]
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
    ap.add_argument('--repo', default='ptr727/ProjectTemplate')
    ap.add_argument('--timeout', type=int, default=2700, help='seconds (default 45m)')
    ap.add_argument('--pickup-grace', type=int, default=300,
                    help='seconds before the first pickup read, and between reads (default 5m)')
    a = ap.parse_args(argv)
    # A negative grace leaves the next reading permanently behind the clock.
    # That is the per-poll REST pattern the interval exists to prevent.
    if a.pickup_grace < 0:
        ap.error('--pickup-grace cannot be negative')
    owner, repo = a.repo.split('/', 1)

    if a.cmd == 'status':
        out, _ = digest(owner, repo, a.number)
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
    out, _ = digest(owner, repo, a.number, pr=final, stalled=stalled)
    print(out)
    print(f'waited={int(time.monotonic()-start)}s')
    if reviewed_head(final):
        return 0
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
