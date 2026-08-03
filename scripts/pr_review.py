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
           A quota or rate-limit refusal is terminal: no review lands and waiting on
           makes no difference, so 40 ends the wait rather than extending it.

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

# Liveness query: timestamps and ids only, no comment or review bodies.
# A liveness check does not need the finding text, and re-fetching bodies was 76% of polls.
# It does need the reviewer's non-review answers.
# A wait reading formal reviews alone treats a refusal as an unmet condition.
Q_LIVE = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    headRefOid
    reviews(last:20){ nodes{ author{login} state commit{oid} submittedAt } }
    comments(last:5){ nodes{ author{login} createdAt } }
  }}}
"""

# Full query: run once on transition, not per poll.
Q_FULL = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    headRefOid mergeable mergeStateStatus
    reviews(last:20){ nodes{ author{login} state commit{oid} submittedAt body } }
    reviewThreads(first:100){ nodes{ id isResolved
      comments(first:1){ nodes{ author{login} path line body } } }}
    comments(last:5){ nodes{ author{login} createdAt body } }
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


def reviewer_nodes(pr: dict, field: str) -> list[dict]:
    """The reviewer's own nodes under `field`, oldest first as the API returns them."""
    return [n for n in ((pr.get(field) or {}).get('nodes') or [])
            if (n.get('author') or {}).get('login') == REVIEWER]


def answered_outside_review(pr: dict) -> dict | None:
    """The reviewer's newest plain comment, where it postdates its newest formal review.

    Copilot answers a request with a comment rather than a review when it will not review at all,
    a quota refusal among them, and that answer satisfies no coverage check by design.
    Reading it as an unmet condition is what turns a refusal into a wait with nothing at its end.
    A comment older than the newest review is spent, since the review it preceded did land.
    """
    comments = reviewer_nodes(pr, 'comments')
    if not comments:
        return None
    newest = max(comments, key=lambda n: n.get('createdAt') or '')
    reviews = reviewer_nodes(pr, 'reviews')
    latest_review = max((n.get('submittedAt') or '' for n in reviews), default='')
    return newest if (newest.get('createdAt') or '') > latest_review else None


def live_state(owner: str, repo: str, num: int) -> tuple[str, bool, dict | None]:
    """Return (head_sha, copilot_reviewed_current_head, copilot_answer_outside_a_review)."""
    pr = gql(Q_LIVE, owner, repo, num)
    head = pr['headRefOid']
    done = any((n.get('commit') or {}).get('oid') == head
               for n in reviewer_nodes(pr, 'reviews'))
    return head, done, answered_outside_review(pr)


def heading_of(block: str) -> str:
    """The block's `<summary>`, or its opening where the wrapper carries none."""
    m = SUMMARY.search(block)
    return m.group(1) if m else block[:200]


def suppressed_blocks(body: str) -> list[str]:
    """Return the review body's low-confidence sections, matched on their heading.

    The heading carries the match rather than the body text, since a review whose prose discusses
    suppressed findings is not itself carrying any. The fallback covers the day the `<details>`
    wrapper moves, and takes a heading with a count so ordinary prose is not read as one.
    """
    if not body:
        return []
    blocks = [b for b in DETAILS.findall(body) if SUPPRESSED.search(heading_of(b))]
    if blocks:
        return blocks
    outside = DETAILS.sub('', body).splitlines()
    for i, line in enumerate(outside):
        if SUPPRESSED.search(line) and COUNT.search(line):
            return ['\n'.join(outside[i:])]
    return []


def finding_count(block: str) -> int:
    """The heading's `(N)`, floored at one, since a block reported as zero reads as a clean pass."""
    m = COUNT.search(heading_of(block))
    return max(int(m.group(1)), 1) if m else 1


def digest(owner: str, repo: str, num: int, seen: set[str] | None = None) -> tuple[str, int]:
    pr = gql(Q_FULL, owner, repo, num)
    head = pr['headRefOid']
    revs = reviewer_nodes(pr, 'reviews')
    on_head = [n for n in revs if (n.get('commit') or {}).get('oid') == head]
    threads = pr['reviewThreads']['nodes']
    unresolved = [t for t in threads
                  if not t['isResolved']
                  and ((t.get('comments') or {}).get('nodes') or [{}])[0]
                  .get('author', {}).get('login') == REVIEWER]

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
    lines = [
        f'pr={num} head={head[:8]} rounds={len(revs)} '
        f'review_on_head={"yes" if on_head else "NO"} '
        f'threads={len(threads)} unresolved={len(unresolved)} '
        f'suppressed={sum(finding_count(b) for n, b in blocks)} '
        f'(on_head={sum(finding_count(b) for b in on_head_blocks)} earlier={stale}) '
        f'answered_outside_review={"yes" if answer else "no"} '
        f'merge={pr.get("mergeStateStatus")}'
    ]
    if answer:
        # Printed whole for the same reason a suppressed finding is, since it reaches no thread.
        # Its wording is the only thing separating a refusal from an ordinary remark.
        lines.append(f'  COPILOT COMMENT ({answer.get("createdAt")}, newer than any review): '
                     'read it before waiting again, since a quota or rate-limit refusal is '
                     'terminal and no review follows it')
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
    a = ap.parse_args(argv)
    owner, repo = a.repo.split('/', 1)

    if a.cmd == 'status':
        out, _ = digest(owner, repo, a.number)
        print(out)
        return 0

    # In-process backoff, so the whole wait costs one agent turn.
    delays = [15, 20, 30, 45, 60, 120]
    start = time.monotonic()
    _, done, answer = live_state(owner, repo, a.number)
    i = 0
    while not done and not answer:
        if time.monotonic() - start > a.timeout:
            # The timeout is where the digest's one extra call is worth most.
            # A bare PENDING line reports a broken wait and a slow reviewer identically.
            out, _ = digest(owner, repo, a.number)
            print(out)
            print(f'status=PENDING waited={int(time.monotonic()-start)}s')
            return 30
        time.sleep(delays[min(i, len(delays) - 1)])
        i += 1
        # Re-read head each iteration: a push during the wait moves it.
        _, done, answer = live_state(owner, repo, a.number)
    out, _ = digest(owner, repo, a.number)
    print(out)
    print(f'waited={int(time.monotonic()-start)}s')
    if not done:
        print('status=ANSWERED_OUTSIDE_REVIEW read the comment above, '
              'a quota or rate-limit refusal is terminal and re-requesting does not clear it')
        return 40
    return 0


if __name__ == '__main__':
    sys.exit(main())
