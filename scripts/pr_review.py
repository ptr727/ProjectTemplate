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
           Exit 0 = review present, 30 = still pending at timeout (pending is not failure).

Read-only by design. Mutations (re-request review, reply, resolve thread) are
deliberately NOT implemented here - they are state-changing calls that must stay
visible to the gh-write-guard PreToolUse hook and to review. See
.github/copilot-instructions.md for the mutation runbook.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, time

REVIEWER = 'copilot-pull-request-reviewer'

# A review body can carry a collapsed block of findings withheld from the inline threads.
# Those appear nowhere in `reviewThreads`, so polling threads alone reports a clean pass while
# they stand. The alternation is the runbook's, because the heading wording has changed once
# already, and matching one phrasing alone reports zero on a review that has them.
SUPPRESSED = re.compile(r'Suppressed comments|low confidence', re.IGNORECASE)
DETAILS = re.compile(r'<details>(.*?)</details>', re.DOTALL | re.IGNORECASE)
SUMMARY = re.compile(r'<summary>(.*?)</summary>', re.DOTALL | re.IGNORECASE)
TAGS = re.compile(r'</?(?:details|summary)>', re.IGNORECASE)
COUNT = re.compile(r'\((\d+)\)')

# Liveness query: two scalars only, no comment bodies.
# A liveness check does not need the finding text, and re-fetching bodies was 76% of polls.
Q_LIVE = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    headRefOid
    reviews(last:20){ nodes{ author{login} state commit{oid} } }
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


def live_state(owner: str, repo: str, num: int) -> tuple[str, bool]:
    """Return (head_sha, copilot_reviewed_current_head)."""
    pr = gql(Q_LIVE, owner, repo, num)
    head = pr['headRefOid']
    done = any((n.get('author') or {}).get('login') == REVIEWER
               and (n.get('commit') or {}).get('oid') == head
               for n in pr['reviews']['nodes'])
    return head, done


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
    revs = [n for n in pr['reviews']['nodes']
            if (n.get('author') or {}).get('login') == REVIEWER]
    on_head = [n for n in revs if (n.get('commit') or {}).get('oid') == head]
    threads = pr['reviewThreads']['nodes']
    unresolved = [t for t in threads
                  if not t['isResolved']
                  and ((t.get('comments') or {}).get('nodes') or [{}])[0]
                  .get('author', {}).get('login') == REVIEWER]

    # Scoped to the head, so a finding answered in an earlier round does not re-open.
    blocks = [b for n in on_head for b in suppressed_blocks(n.get('body') or '')]

    lines = [
        f'pr={num} head={head[:8]} rounds={len(revs)} '
        f'review_on_head={"yes" if on_head else "NO"} '
        f'threads={len(threads)} unresolved={len(unresolved)} '
        f'suppressed={sum(finding_count(b) for b in blocks)} '
        f'merge={pr.get("mergeStateStatus")}'
    ]
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
    for b in blocks:
        # Printed whole where a thread body is truncated: a thread can be re-read at its id,
        # while a suppressed finding has no thread, so this digest is the only place it appears.
        lines.append('  SUPPRESSED: no thread to resolve, answer it in the PR conversation')
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
    head0, done = live_state(owner, repo, a.number)
    i = 0
    while not done:
        if time.monotonic() - start > a.timeout:
            print(f'pr={a.number} head={head0[:8]} review_on_head=NO '
                  f'status=PENDING waited={int(time.monotonic()-start)}s')
            return 30
        time.sleep(delays[min(i, len(delays) - 1)])
        i += 1
        # Re-read head each iteration: a push during the wait moves it.
        head0, done = live_state(owner, repo, a.number)
    out, _ = digest(owner, repo, a.number)
    print(out)
    print(f'waited={int(time.monotonic()-start)}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
