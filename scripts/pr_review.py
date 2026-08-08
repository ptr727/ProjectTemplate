#!/usr/bin/env python3
"""Consolidated Copilot-review status for a PR - one command, one compact digest.

Why this exists: 4,513 of the measured `gh` invocations were one call per agent turn, and
each turn re-bills the whole session context. The bytes `gh` returns are trivial, averaging
574, so the round-trips are the entire cost. This collapses a poll cycle into a single
invocation whose output is a few hundred bytes. See GOVERNANCE.md "Context and Delegation
Discipline" for the rule this implements.

Subcommands
  claims   Check the description against the branch it describes. A body claiming a commit or
           quoting a `uses:` ref the head tree no longer carries is a silent failure caught by a
           reviewer or not at all, and three stale descriptions in one session generated six
           review findings between them. Read-only. Exit 0 = every reference resolves, 70 = one
           does not, 71 = there were references and none could be read, so nothing was decided.
  status   One digest line, any unresolved threads, and any suppressed findings. Read-only.
           Exit 0 = every shape in the reviewer's output is one this reads, and the round
           covering the head read the whole diff or stated nothing about what it read.
           42 = that round read fewer files than the pull request changed, so part of the diff
           has no review at all, and the remedy is a re-request or a split.
           43 = the reviewer sent a shape this script has no reader for, so no field here can
           be believed. The remedy is an issue on the repository hosting this script, and the
           review loop does not close until the reader is fixed. Merging regardless is the
           maintainer's decision rather than the agent's.
  reply    Answer one thread selected by its text, and resolve it on request. The only
           writing path here, and it exists because the hand-run form keeps failing the
           same way: a node id typed into a mutation, which resolves globally and so
           writes to a real thread somewhere rather than failing. This takes a pull
           request number and words from the finding, queries the id itself, and offers
           no argument an id fits in. Exit 0 = done, 60 = no thread matched, 61 = more
           than one did, 62 = the reply returned no comment url so nothing was resolved,
           63 = the resolve did not report the thread resolved, 64 = the target is under
           another owner.
  wait     Poll until Copilot's review lands on the current head, then print the digest.
           The loop runs in-process, so a 45-minute wait costs one agent turn, not 90.
           Exit 0 = review present, 30 = still pending at timeout (pending is not failure),
           40 = Copilot answered outside a formal review, so read the printed body.
           40 reports the shape of that answer and reads nothing of its cause: an answer
           carrying no commit covers no head, so the wait ends and the reader decides.
           41 = the review carrying the head says it did not review, so it covers nothing.
           42 and 43 = the review landed and `status`'s two blocking readings apply to it,
           since a wait ending on a round that covered half the diff, or on output nothing here
           can read, has ended on something other than a review of this pull request.
           50 = the request is pending and nothing picked it up, which no amount of
           waiting changes. Recovery is two mutations, and they stay in the runbook.

Reading is the bulk of this and `reply` is the one exception, which is a trade rather
than a free win. A mutation spelled as a `gh` command in a shell is read by the
gh-write-guard PreToolUse hook, and one this script performs is not, since the hook sees
`python3 pr_review.py reply` and no gh write. What the hook guards against there is a
fabricated id, and that is the failure this removes at the source instead: the id is
never in the caller's hands to fabricate. Re-requesting a review stays out, having no
such failure and no id to hide. See .github/copilot-instructions.md for the runbook, and
GOVERNANCE.md "Repository Boundaries and Write Safety" for the rules `reply` enforces.
"""
from __future__ import annotations
import argparse, io, json, re, subprocess, sys, tarfile, time
from pathlib import Path

REVIEWER = 'copilot-pull-request-reviewer'

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
# A round states how much of the diff it read on a line of its own.
# A round that read part of it is the clean pass elsewhere, same commit and threads and digest.
# Five such rounds landed across three merged pull requests here.
# One of them read 2 of 3 changed files across both its rounds and merged.
# This is the third instance of the shape the two patterns above answer.
# It is also the only one nothing was reading.
# The line is anchored at its start rather than matched body-wide, both spellings being structural.
# Over 332 review bodies every coverage statement opens its line and not one sits mid-sentence.
# 272 of them open with the reviewer's own name and 32 are the `Review details` bullet.
# A body-wide match reports the pull request adding this check as a partial round.
# That is the false positive the suppressed matcher and the refusal matcher have each had once.
# The cost of the anchor is named rather than hidden.
# A wording that moves the statement off the line start reads as no statement at all.
# Two openers rather than one alternation, because the text each needs beside it differs.
# The bullet's own label is the marker, so it is a coverage line whatever follows the counts.
# Requiring the trailing words there made a bullet that drops them read as no statement at all.
# The sentence opener is the reviewer's name, which prose also opens a line with.
# That one keeps the text requirement, since the name alone does not say the line states coverage.
COVERAGE_BULLET = re.compile(r'\s*[-*]\s*\*\*Files reviewed:', re.IGNORECASE)
COVERAGE_SENTENCE = re.compile(r'\s*Copilot\b', re.IGNORECASE)
# The count pair itself, in the two spellings the corpus carries.
# The comment tail one of them ends on is deliberately not part of the unit.
# It says how many comments the round raised, which is not coverage.
# A fifth wording of it would fail every merge over a sentence ending read correctly.
# The plural is optional, since a one-file round reading `1 changed file` means what it says.
# Blocking on that is the cry-wolf case, a fleet-wide stop over a grammatical agreement.
# The bullet's trailing words are optional for the reason its label alone identifies the line.
# Detection and parsing disagreeing there turned a readable `4/4` into a block on a readable line.
# What is left blocking is a bullet carrying no counts, which genuinely states no coverage.
COVERAGE_COUNTS = re.compile(
    r'reviewed\s+(\d+)\s+out of\s+(\d+)\s+changed files?'
    r'|\*\*Files reviewed:\*\*\s*(\d+)\s*/\s*(\d+)(?:\s+changed files?)?', re.IGNORECASE)
# A fenced block is a quotation rather than a statement, and 131 of those bodies carry one.
# This change puts both spellings into the source and the runbook, so a review of it quotes them.
# A quoted count read as this round's own is a coverage figure nobody stated.
FENCE = re.compile(r'^ {0,3}```.*?^ {0,3}```[^\n]*', re.DOTALL | re.MULTILINE)
# The readings a round's coverage carries, worst first.
# A head carries more than one round only through a re-request.
# Where two disagree, the one naming files it did not read is the one to answer.
# `UNSTATED` sits last rather than beside the failures, being the absence of a statement.
# A round that did state full coverage settles the question over one that stated nothing.
UNVETTED, PARTIAL, FULL, UNSTATED = 'unvetted', 'partial', 'full', 'unstated'
SEVERITY = (UNVETTED, PARTIAL, FULL, UNSTATED)
# Upper-case for the two that block a merge, for the reason `review_on_head=NO` is upper-case.
# `unstated` rather than `unknown`, since a body carrying no count is a shape this knows.
# What this script does not know is the separate `shapes` field, and one word for both hides it.
# The constant carries that name too, so no reader has to map a name here onto another word.
COVERAGE_FIELD = {UNVETTED: 'UNVETTED', PARTIAL: 'PARTIAL', FULL: 'full',
                  UNSTATED: 'unstated'}

# Every structural marker the reviewer's own bodies carry, measured over the same 332.
# A body is read for these rather than trusted, because every reader below keys on one of them.
# A heading this script has no spelling for is a section it will not find, reported as absent.
# That is the shape of all three failures already on record here, each caught after it landed.
# The lists are small because the output is regular: 7 headings, 6 summaries and 3 labels.
# Counts are normalized to `(N)` and non-ASCII is dropped before comparing.
# The verdict headings carry a colored circle, so the emoji is what would drift most cheaply.
# Dropping it also keeps this file inside the charset rule that governs the repository.
VETTED_HEADINGS = {
    '## Pull request overview', '### Reviewed changes', '### Ready to approve',
    '### Changes recommended', '### Not ready to approve', '### Human review recommended',
    '### Suppressed comments (N)',
}
VETTED_SUMMARIES = {
    'Pull request overview', 'Show a summary per file', 'File summaries', 'Review details',
    'Suppressed comments (N)', 'Comments suppressed due to low confidence (N)',
}
VETTED_LABELS = {'Files reviewed', 'Comments generated', 'Review effort level'}
MARKDOWN_HEADING = re.compile(r'\s*#{1,6}\s')
# The `Review details` metadata bullets, of which the coverage line is one.
LABEL_LINE = re.compile(r'\s*[-*]\s+\*\*([^*]+):\*\*')
# A login that reads as this reviewer without being the spelling every query here filters on.
# A rename leaves every filter matching nothing, so a review that landed reads as none at all.
# A wait then polls out its whole timeout against a review sitting in plain sight.
# `copilot-swe-agent` is the coding agent rather than the reviewer, and does not match this.
READS_AS_REVIEWER = re.compile(r'copilot.*review', re.IGNORECASE)
# The repository this script is hosted in, named because an unrecognized shape is fixed here.
# It is a literal rather than the pull request's own repository.
# That one is where the shape was seen, not where the reader failing on it lives.
HUB = 'ptr727/ProjectTemplate'
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


# The description and the branch it describes, read together so neither is stale against the other.
# No review or comment connection here, since a description is neither.
Q_CLAIMS = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ headRefOid body }}}
"""

# A `uses:` reference quoted in a description, in the spelling a workflow writes it.
BODY_USES = re.compile(r'uses:\s*(?P<ref>[A-Za-z0-9_.\-]+/[^\s`"\']+@[^\s`"\']+)')
# A commit the description claims this branch carries, being a verb plus a SHA rather than a SHA.
# The free scan was built first and the corpus rejected it outright.
# Over 25 merged pull requests it raised four findings and every one was correct prose.
# Those were a develop commit named as history, a SHA in quoted output, and two in another repo.
# Nothing in the shape of a bare SHA separates one of those from a genuine claim.
# Separating them by meaning is the similarity heuristic `spec/section-model.md` rejects.
# The verb is what makes a SHA a claim about this branch rather than a mention of one.
# This alternation raises exactly one reference over the same 25, and that one is true.
# The vocabulary is an inclusion list, so a phrasing nobody thought of costs a detection.
# Being incomplete in that direction is the safe one, since it never invents a finding.
BODY_CLAIM = re.compile(
    r'\b(?:fixed|landed|shipped|added|introduced|corrected|resolved|carried|amended)'
    r'\s+(?:in|by|as)\s+`?(?P<sha>[0-9a-f]{7,40})`?', re.IGNORECASE)
# What `gh` prints when GitHub answered, as opposed to when nothing was reached at all.
HTTP_STATUS = re.compile(r'\(HTTP (\d{3})\)')
# The two GitHub returns for an object that is not there.
# A network error carries no status, and 401 and 403 are credentials and a rate limit.
# Reading one of those as absence reports a correct description as stale.
# Everything outside this set is therefore "not read" rather than "not there".
ABSENT = {'404', '422'}
# What a reference reads as where GitHub never answered for it.
UNREAD = 'unread'
# Longer than the ordinary read's, since this one downloads a repository rather than a field.
TARBALL_TIMEOUT = 120

# Threads for the reply path, paginated.
# A first page read as the whole set reports no match on a thread that is simply further along.
# `line` is read for the confirmation line rather than for matching, since a push moves it.
# That is how a reply keyed on a line number went to a thread that had shifted underneath it.
Q_THREADS = """
query($o:String!,$r:String!,$n:Int!,$after:String){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    reviewThreads(first:100, after:$after){
      nodes{ id isResolved path line comments(first:1){ nodes{ author{login} body } } }
      pageInfo{ hasNextPage endCursor } }
  }}}
"""

# The two mutations the runbook publishes, in the order it publishes them.
# `url` is fetched because it is the one field that says the reply carried a body.
# A reply that posted empty still returns a comment, and three did, each then resolved.
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
    """Run one GraphQL document and return its `data`, raising rather than reporting a blank.

    A string goes through `-f` and an int through `-F`, because `-F` infers a type from the text:
    a reply body of `123` or `true` arrives as an Int or a Boolean and the mutation fails on a
    type nobody passed it, and a body opening with `@` is read as a filename.

    `errors` is checked rather than trusted to the exit code, since a GraphQL document can fail
    per-field while the request itself succeeds, and the caller would read the null that leaves.
    """
    argv = ['gh', 'api', 'graphql', '-f', f'query={query}']
    for name, value in variables.items():
        argv += ['-F' if isinstance(value, int) else '-f', f'{name}={value}']
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[:800])
        raise SystemExit(f'gh graphql failed rc={r.returncode}')
    payload = json.loads(r.stdout)
    if payload.get('errors'):
        sys.stderr.write(json.dumps(payload['errors'])[:800])
        raise SystemExit('gh graphql reported errors')
    return payload['data']


def gql(query: str, owner: str, repo: str, num: int) -> dict:
    return gh_graphql(query, o=owner, r=repo, n=num)['repository']['pullRequest']


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


def head_reviews(pr: dict) -> list[dict]:
    """The reviewer's own reviews that cover the current head, refusals excluded.

    A refusal is not coverage. It is a formal review, `state: COMMENTED`, carrying the head's
    commit and raising no threads, so it satisfies every check a clean pass does and renders a
    digest identical to one. That is how a pull request of 301 changed files, one over the
    reviewer's limit, sat one command from merging on a review that never ran.

    One list rather than a predicate beside a filter, because coverage and the count of rounds on
    the head are read from the same set and a second spelling of it drifts from the first.
    """
    head = pr['headRefOid']
    return [n for n in reviewer_nodes(pr, 'reviews')
            if (n.get('commit') or {}).get('oid') == head and not refusal_of(n)]


def reviewed_head(pr: dict) -> bool:
    """True where one of the reviewer's own reviews covers the current head's commit.

    The liveness query carries no bodies, so a refusal reads there as ordinary coverage. That is
    deliberate rather than a gap: it ends the wait, which is what a terminal outcome should do,
    and the full read every wait finishes with is what tells the two apart. No exit code and no
    merge decision is taken from the liveness reading.
    """
    return bool(head_reviews(pr))


def is_coverage_line(line: str) -> bool:
    """Whether this line is the reviewer stating its file coverage, rather than prose about it."""
    if COVERAGE_BULLET.match(line):
        return True
    return bool(COVERAGE_SENTENCE.match(line)) and 'changed file' in line.lower()


def coverage_statements(body: str) -> list[str]:
    """The lines this round states its file coverage on, quotations excluded."""
    return [ln.strip() for ln in FENCE.sub('', body or '').splitlines() if is_coverage_line(ln)]


def read_coverage(line: str) -> tuple[int, int] | None:
    """The (reviewed, changed) counts the line states, or None where they cannot be believed.

    None covers a wording this has no vetted spelling for and a pair that cannot both be true
    alike, since each leaves the same question unanswered and each takes the same remedy. A round
    reporting it read more files than the pull request changed is not a round that read them all,
    it is a line this script is parsing wrongly, and reading it as full coverage fails open on
    exactly the statement that says something is off.
    """
    m = COVERAGE_COUNTS.search(line)
    if not m:
        return None
    reviewed, changed = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return (int(reviewed), int(changed)) if int(reviewed) <= int(changed) else None


def coverage_of(node: dict) -> tuple[str, str]:
    """This round's coverage reading, with the line it was read from.

    A round making no statement at all reads as unstated rather than a pass or a failure. 28 of
    the 332 bodies measured carry an overview and a change list and nothing more, that shape
    interleaves with the counted one throughout rather than preceding it, and one pull request
    carries both across its two rounds. Failing on it would cry wolf on roughly one review in
    twelve, and a guard an agent learns to work around is worse than no guard. Passing it as
    coverage is the bug this whole reading exists to remove, one shape over.

    A wording that is coverage-shaped and parses to no counts is the failure whose remedy is
    fixing this script, since a gate that allows whatever it does not recognize stops gating as
    the wording drifts, which it has done once already for each of the two patterns above.
    """
    worst, detail = UNSTATED, ''
    for line in coverage_statements(node.get('body') or ''):
        counts = read_coverage(line)
        state = UNVETTED if counts is None else (FULL if counts[0] == counts[1] else PARTIAL)
        if SEVERITY.index(state) < SEVERITY.index(worst):
            worst, detail = state, line
    return worst, detail


def head_coverage(pr: dict) -> tuple[str, str]:
    """The worst coverage the rounds covering the current head state, and the line saying it.

    Head-scoped for the reason a refusal is, and unlike a suppressed finding: a partial round is
    a statement about one commit's diff, and the push that changes that diff raises a round which
    reads the whole of the new one. A refusal needs no exemption of its own here, `head_reviews`
    having dropped it already, and reading one would report the round that declined as a wording
    this script fails to recognize.
    """
    worst, detail = UNSTATED, ''
    for node in head_reviews(pr):
        state, line = coverage_of(node)
        if SEVERITY.index(state) < SEVERITY.index(worst):
            worst, detail = state, line
    return worst, detail


def normal(text: str) -> str:
    """A marker reduced to what a vetted list compares: ASCII, single spaces, counts as `(N)`.

    The verdict headings carry a colored circle and the suppressed heading carries its finding
    count, so both drift on every review without the section having changed at all.
    """
    ascii_only = ''.join(c for c in text if ord(c) < 128)
    return re.sub(r'\s+', ' ', re.sub(r'\(\d+\)', '(N)', ascii_only)).strip()


def unrecognized_in(body: str) -> list[str]:
    """Every marker in one review body this script has no vetted spelling for.

    Read over the body with fenced blocks removed, for the reason the coverage line is: a review
    quoting a heading is not a review carrying one, and this script's own pull requests quote
    these lists in full.

    A body carrying no heading at all is reported rather than passed, since every one of the 332
    measured opens on a heading and a body with none is a format nothing here has seen. A refusal
    is the exemption, being a bare paragraph by design and already classified as one.
    """
    # A refusal is a bare paragraph rather than a review body, and `REFUSAL` is its spelling.
    # It carries no marker to check, and its own opening line is not a coverage statement.
    # Its wording drifting is still caught, since `refusal_of` then stops matching.
    # What is left of a drifted refusal is a body with no heading, which is the arm below.
    if refusal_of({'body': body}):
        return []
    plain = FENCE.sub('', body or '')
    headings = [normal(ln) for ln in plain.splitlines() if MARKDOWN_HEADING.match(ln)]
    labels = [normal(m.group(1)) for m in map(LABEL_LINE.match, plain.splitlines()) if m]
    found = [f'heading: {h}' for h in dict.fromkeys(headings) if h not in VETTED_HEADINGS]
    found += [f'summary: {normal(s)}' for s in dict.fromkeys(SUMMARY.findall(plain))
              if normal(s) not in VETTED_SUMMARIES]
    found += [f'metadata label: {la}' for la in dict.fromkeys(labels) if la not in VETTED_LABELS]
    found += [f'coverage line: {ln}' for ln in coverage_statements(body)
              if read_coverage(ln) is None]
    if not headings and not refusal_of({'body': body}):
        found.append('body carrying no heading at all, which no measured review body does')
    return found


def unrecognized_shapes(pr: dict) -> list[str]:
    """Everything about this pull request's reviewer output that this script cannot read.

    Every round rather than the head's, because this asks whether the reader still understands
    the reviewer rather than what the reviewer said about this commit. A shape that arrived one
    round ago is one every later round will carry.
    """
    found = []
    for node in reviewer_nodes(pr, 'reviews'):
        where = ((node.get('commit') or {}).get('oid') or '')[:8] or 'commit unknown'
        found += [f'{item}  (round {where})' for item in
                  unrecognized_in(node.get('body') or '')]
    logins = {(n.get('author') or {}).get('login') or ''
              for field in ('reviews', 'comments')
              for n in ((pr.get(field) or {}).get('nodes') or [])}
    found += [f'reviewer login: {login}, where every query here filters on {REVIEWER}'
              for login in sorted(logins)
              if login != REVIEWER and READS_AS_REVIEWER.search(login)]
    return found


def report_verdict(pr: dict) -> int:
    """Print the blocking verdict's own status line, and return the exit code it carries.

    The unrecognized shape outranks the coverage one, because a reader that does not understand
    the output cannot be trusted about what it read of the diff either.
    """
    # A coverage line this cannot parse is one of the shapes below rather than a case of its own.
    # It exits here with the remedy that fits it, the reader being what needs the fix.
    if unrecognized_shapes(pr):
        print(f'status=UNRECOGNIZED_REVIEWER_OUTPUT this script does not know one or more shapes '
              f'in what the reviewer sent, listed above, so nothing it reports about this review '
              f'is trustworthy and the review loop does not close on this digest. File an issue '
              f'on {HUB} naming each shape and quoting the body it came from, since this script '
              f'is hosted there and the fix lands there. Merging this pull request anyway is the '
              f'maintainer\'s decision to take and not this script\'s, and not the agent\'s.')
        return 43
    state, _ = head_coverage(pr)
    if state == PARTIAL:
        print('status=COVERAGE_IS_PARTIAL the review covering the head read fewer files than the '
              'pull request changed, so part of the diff has no review at all: re-request on this '
              'head, or split the pull request, rather than merging it')
        return 42
    return 0


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
    # `revs` is every round and `on_head` is the ones that reviewed this commit.
    # A refusal sits in the first and not the second, being a round that covered nothing.
    on_head = head_reviews(pr)
    cover, cover_line = head_coverage(pr)
    unknown = unrecognized_shapes(pr)
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
    lines = [
        # The repository leads the line, since a number alone reads as correct anywhere.
        # A digest of the wrong pull request is well-formed, so naming it is what shows the miss.
        f'repo={owner}/{repo} pr={num} head={head[:8]} rounds={len(revs)} '
        f'review_on_head={"yes" if on_head else "NO"} '
        # A field of its own beside that one, since a round can cover the head and read part.
        # Those two readings are what `review_on_head=yes` alone conflates.
        f'coverage={COVERAGE_FIELD[cover]} '
        # Every other field on this line is a reading of the review.
        # This one says whether the readings can be believed at all, so it is not a count.
        f'shapes={"UNRECOGNIZED" if unknown else "ok"} '
        # A field of its own, since `rounds=1 review_on_head=NO` is also what a stale round is.
        # The two want opposite responses, one a re-request and the other a split pull request.
        # Upper-case for the reason `NO` is, as a state that blocks a merge is not one to skim.
        f'refusal={"YES" if refusal else "no"} '
        f'threads={len(threads)} unresolved={len(unresolved)} '
        f'suppressed={sum(finding_count(b) for n, b in blocks)} '
        f'(on_head={sum(finding_count(b) for b in on_head_blocks)} earlier={stale}) '
        f'answered_outside_review={answered} '
        f'requested={"yes" if reviewer_requested(pr) else "no"} '
        f'merge={pr.get("mergeStateStatus")}'
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
    if unknown:
        # First of the blocks, since it says how far the rest of them can be trusted.
        lines.append(f'  UNRECOGNIZED REVIEWER OUTPUT ({len(unknown)}): the shapes below are ones '
                     'this script has no reader for, so every other field here is a reading of '
                     'output it does not fully understand and a clean digest does not mean a '
                     f'clean review. File an issue on {HUB}, which hosts this script, naming each '
                     'shape and quoting the body it came from, before closing the review loop. '
                     'Whether to merge anyway is the maintainer\'s call rather than the agent\'s')
        lines += [f'    {item}' for item in unknown]
    if cover == PARTIAL:
        # The line prints under the marker for the reason a suppressed block does.
        # The counts say how much of the diff went unread, and no thread carries them.
        lines.append('  COVERAGE IS PARTIAL: the review covering the head read fewer files than '
                     'the pull request changed, so files in the diff have no review at all and '
                     'the remedy is a re-request or a split rather than a merge')
        lines.append(f'    {cover_line}')
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


def origin_owner() -> str | None:
    """The owner of the checkout this script sits in, or None where that cannot be read.

    Anchored on the script's own directory rather than the working directory, because this is
    reached from a hub checkout while the repository being answered is named on the command line,
    so the working directory says nothing about who owns either.
    """
    try:
        url = subprocess.run(['git', '-C', str(Path(__file__).resolve().parent),
                              'remote', 'get-url', 'origin'],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    m = re.search(r'[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$', url)
    return m.group(1).lower() if m else None


def in_scope(target_owner: str) -> tuple[bool, str]:
    """Whether writing to `target_owner` is in scope here, with the reason where it is not.

    Same owner covers the origin and every sibling, which is the whole fleet. A different owner is
    the shape that once put a comment on a stranger's pull request, and it is refused outright
    rather than granted by an environment variable, because a variable this process can be handed
    is one the caller can set on the command that runs it, and a grant the caller writes for
    itself is not a grant. The `gh-write-guard` hook reads that grant from the session it was
    launched with, which is why the cross-owner case belongs on the runbook's explicit `gh` path
    where the hook is the one adjudicating it.
    """
    origin = origin_owner()
    if origin is None:
        return False, ('this checkout has no readable `origin`, so the owner a write would stay '
                       'within cannot be established, and an unverified scope is not a scope')
    if target_owner.lower() != origin:
        return False, (f'the target is under {target_owner}, and this checkout is under {origin}. '
                       'A different owner is the shape this refuses outright: take it through the '
                       'runbook mutations, where the write-guard hook reads the maintainer grant')
    return True, ''


def first_comment(thread: dict) -> dict:
    """The thread's opening comment, which is the finding itself."""
    return ((thread.get('comments') or {}).get('nodes') or [{}])[0]


def unresolved_threads(owner: str, repo: str, num: int) -> list[dict]:
    """Every unresolved review thread, following the cursor to the end.

    Stopping at the first page reports no match on a thread that is merely further along, and a
    no-match is indistinguishable from a thread that was already answered.
    """
    out: list[dict] = []
    after = None
    while True:
        extra: dict[str, str] = {'after': after} if after else {}
        conn = gh_graphql(Q_THREADS, o=owner, r=repo, n=num,
                          **extra)['repository']['pullRequest']['reviewThreads']
        out += [t for t in conn['nodes'] if not t['isResolved']]
        page = conn.get('pageInfo') or {}
        if not page.get('hasNextPage'):
            return out
        after = page['endCursor']


def describe(thread: dict) -> str:
    """One line naming a thread by what a reader recognizes it as, never by its id."""
    c = first_comment(thread)
    body = ' '.join((c.get('body') or '').split())
    return (f'{thread.get("path")}:{thread.get("line")} '
            f'by {(c.get("author") or {}).get("login")}: {body[:120]}')


def matching_threads(threads: list[dict], match: str, path: str | None) -> list[dict]:
    """Threads whose finding text contains `match`, narrowed by `path` where one is given.

    Matched on the finding's own words rather than on a line number, because a fix push moves the
    line and every lookup keyed to one then misses: replies posted against nothing while the
    resolves still succeeded, so the threads closed carrying no answer. Case-insensitive, since
    the text is quoted back out of a digest by a reader rather than compared by a machine.
    """
    needle = match.lower()
    return [t for t in threads
            if needle in (first_comment(t).get('body') or '').lower()
            and (path is None or t.get('path') == path)]


def reply_to_thread(owner: str, repo: str, num: int, match: str, body: str,
                    path: str | None, resolve: bool) -> int:
    """Answer the one thread `match` selects, and resolve it where asked. Returns an exit code.

    Every refusal below is a stop rather than a fallback. There is no id to guess at, no
    second-best thread to settle for, and no resolve on a reply that did not land, because each
    of those closes a finding while leaving it unanswered, which is the state a reviewer reads as
    addressed.
    """
    ok, why = in_scope(owner)
    if not ok:
        print(f'status=OUT_OF_SCOPE nothing was written: {why}')
        return 64

    threads = unresolved_threads(owner, repo, num)
    hits = matching_threads(threads, match, path)
    if not hits:
        print(f'status=NO_MATCH nothing was written: no unresolved thread on {owner}/{repo} '
              f'#{num} carries {match!r}'
              + (f' at {path}' if path else '')
              + '. Widen the words or drop --path rather than reaching for an id, since the '
                'thread may also be resolved already, which reads the same from here.')
        for t in threads:
            print(f'  unresolved: {describe(t)}')
        return 60
    if len(hits) > 1:
        print(f'status=AMBIGUOUS nothing was written: {len(hits)} unresolved threads carry '
              f'{match!r}, and picking one of them is the failure this avoids rather than a '
              'default it can take. Quote more of the finding, or add --path.')
        for t in hits:
            print(f'  candidate: {describe(t)}')
        return 61

    target = hits[0]
    print(f'answering: {describe(target)}')
    reply = (gh_graphql(M_REPLY, threadId=target['id'], body=body)
             .get('addPullRequestReviewThreadReply') or {})
    comment = reply.get('comment') or {}
    # The url is what says a reply carried a body, and an empty one still returns a comment.
    # Resolving past that closes the thread with nothing in it, which is what happened three times.
    if not comment.get('url') or not (comment.get('body') or '').strip():
        print('status=REPLY_NOT_CONFIRMED the reply returned no url or an empty body, so the '
              'thread is NOT resolved and the answer is not recorded. Read the response above '
              'before retrying, since a write that appears to fail may have taken on the server.')
        print(f'  response: {json.dumps(reply)[:400]}')
        return 62
    print(f'replied: {comment["url"]}')

    if not resolve:
        print('status=REPLIED the thread is answered and left open, since --resolve was not '
              'given. A decline is resolved only once its evidence is in the thread.')
        return 0

    thread = ((gh_graphql(M_RESOLVE, threadId=target['id']).get('resolveReviewThread') or {})
              .get('thread') or {})
    if not thread.get('isResolved'):
        print('status=RESOLVE_NOT_CONFIRMED the reply landed and the resolve did not report the '
              'thread resolved, so it is still open and the answer is already posted.')
        print(f'  response: {json.dumps(thread)[:400]}')
        return 63
    print('status=REPLIED_AND_RESOLVED')
    return 0


def gh_rest(path: str, jq: str | None = None) -> subprocess.CompletedProcess:
    """One REST read, returned whole so the caller can tell an absent object from an unread one.

    Unlike `gh_graphql` this does not raise on a non-zero exit, because a 404 here is an answer
    the caller acts on rather than a failure. Reads only: every path passed in is a GET.
    """
    argv = ['gh', 'api', path] + (['--jq', jq] if jq else [])
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 1, '', 'gh could not be run')


def answered_absent(proc: subprocess.CompletedProcess) -> bool:
    """Whether GitHub said the object is not there, as opposed to nothing having been read."""
    m = HTTP_STATUS.search(proc.stderr)
    return bool(m) and m.group(1) in ABSENT


def body_references(body: str) -> tuple[list[str], list[str]]:
    """The `uses:` refs and the claimed commits a description quotes, de-duplicated and sorted.

    A commit counts only where a verb claims this branch carries it. A SHA mentioned without one
    is a mention of history, of another repository, or of quoted output, and the corpus above says
    that is what a description's SHAs almost always are.

    Prose claims stay out for the same reason the free SHA scan did. Judging those needs a
    similarity heuristic, which `spec/section-model.md` rejects.

    A claimed SHA still has to carry a digit, as a backstop on the vocabulary above: `accede` and
    `defaced` inflect into all-hex English words, so a verb this list gains later cannot start
    reading one of them as a commit. It costs about one real SHA in a thousand.
    """
    uses = sorted({m.group('ref') for m in BODY_USES.finditer(body)})
    shas = sorted({m.group('sha') for m in BODY_CLAIM.finditer(body)
                   if any(c.isdigit() for c in m.group('sha'))})
    return uses, shas


def commit_state(owner: str, repo: str, sha: str, head: str) -> str:
    """The empty string where `head` carries `sha`, `UNREAD` where nothing was read, else why not.

    Ancestry rather than membership of the branch's own commits, so a description may cite a
    commit it inherited from the base branch. GitHub reports the comparison from the base's side,
    so `identical` and `ahead` are the two readings that say the head carries it.
    """
    proc = gh_rest(f'repos/{owner}/{repo}/compare/{sha}...{head}', '.status')
    if proc.returncode == 0:
        status = proc.stdout.strip()
        if status in ('identical', 'ahead'):
            return ''
        return (f'{owner}/{repo} carries the commit and this head does not descend from it '
                f'({status})')
    if answered_absent(proc):
        return f'{owner}/{repo} carries no such commit'
    return UNREAD


def head_carries(owner: str, repo: str, head: str, refs: list[str]) -> set[str] | None:
    """Which of `refs` appear anywhere in the tree at `head`, or None where nothing was read.

    Read at the head commit rather than from a local checkout, because the branch being described
    need not be fetched here and a working tree is not what a description is measured against.

    The whole tree rather than a guessed set of workflow paths: this repo carries `uses:` lines in
    catalog snippets and in documentation as well as under `.github/`, and a surface narrower than
    the tree would report a ref as absent because it looked in the wrong place. One archive is
    also one request, where walking a listing costs a request per file and grows with the repo.
    """
    # Read as bytes, so this cannot go through `gh_rest` and carries that helper's guards itself.
    # An absent `gh` or a hung download reads as undecided, like every other unreadable answer.
    # Raising instead would abort a run the caller is in the middle of.
    try:
        proc = subprocess.run(['gh', 'api', f'repos/{owner}/{repo}/tarball/{head}'],
                              capture_output=True, timeout=TARBALL_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    needles = {r: r.encode() for r in refs}
    found: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode='r:gz') as archive:
            for member in archive:
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                # Matched as bytes, so a file this cannot decode is searched rather than skipped.
                # No encoding guess can then turn a present ref into an absent one.
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
    head, body = pr['headRefOid'], pr.get('body') or ''
    uses, shas = body_references(body)

    stale, unread = [], 0
    for sha in shas:
        state = commit_state(owner, repo, sha, head)
        if state == UNREAD:
            unread += 1
        elif state:
            stale.append(f'STALE COMMIT `{sha}`: {state}, so the description points at something '
                         'this branch does not have')

    # One read for every ref together, since the archive it reads is the same one either way.
    carried = head_carries(owner, repo, head, uses) if uses else set()
    for ref in uses:
        if carried is None:
            unread += 1
        elif ref not in carried:
            stale.append(f'STALE USES `{ref}`: no file at this head carries that ref')

    print(f'repo={owner}/{repo} pr={num} head={head[:8]} commits={len(shas)} uses={len(uses)} '
          f'stale={len(stale)} unread={unread}')
    for line in stale:
        print(f'  {line}')
    if stale:
        # Named as the description's problem rather than the branch's.
        # The branch is the ground truth here, and the body is what drifted away from it.
        print('status=DESCRIPTION_CONTRADICTS_ITS_BRANCH update the body to what the head tree '
              'carries, since a reference that resolves to nothing is caught by a reviewer or '
              'not at all')
        return 70
    if unread and unread == len(shas) + len(uses):
        # A check that decided nothing prints the same `stale=0` a clean one does.
        print('status=NOTHING_WAS_READ every reference in the description was left undecided '
              'because GitHub did not answer for any of them, so this reports no verdict')
        return 71
    if unread:
        print(f'  NOTE: {unread} reference(s) were left undecided because GitHub did not answer')
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['claims', 'status', 'reply', 'wait'])
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
    # `reply` takes the finding's words rather than its id.
    # There is deliberately no argument an id fits in, so the caller never holds one to mistype.
    ap.add_argument('--match', metavar='TEXT',
                    help='reply: words from the finding, matched against the thread\'s opening '
                         'comment, and required to select exactly one unresolved thread')
    ap.add_argument('--path', metavar='FILE',
                    help='reply: narrow --match to one file, for a file with several findings')
    ap.add_argument('--body', metavar='TEXT',
                    help='reply: the answer to post, carrying the fixing commit SHA or the '
                         'evidence that disproves the finding')
    ap.add_argument('--resolve', action='store_true',
                    help='reply: resolve the thread once the reply is confirmed')
    a = ap.parse_args(argv)
    # Named for the command they belong to, since one silently ignored reads as one that took effect.
    # A `status` given --body reports a clean digest and writes nothing.
    # Nothing in that output says the reply never happened.
    writing = {'--match': a.match, '--path': a.path, '--body': a.body,
               '--resolve': a.resolve or None}
    if a.cmd != 'reply':
        for flag, value in writing.items():
            if value is not None:
                ap.error(f'{flag} belongs to `reply`, not `{a.cmd}`')
    else:
        for flag in ('--match', '--body'):
            if not (writing[flag] or '').strip():
                ap.error(f'reply requires a non-empty {flag}, since a thread resolved on an '
                         'empty answer reads as addressed while carrying nothing')
    # A negative grace leaves the next reading permanently behind the clock.
    # That is the per-poll REST pattern the interval exists to prevent.
    if a.pickup_grace < 0:
        ap.error('--pickup-grace cannot be negative')
    # A bare name is the near-miss a required argument still admits, and unpacking it raises a
    # ValueError traceback rather than saying which half is missing.
    owner, _, repo = a.repo.partition('/')
    if not owner or not repo or '/' in repo:
        ap.error(f'--repo takes OWNER/NAME, not {a.repo!r}')

    if a.cmd == 'claims':
        return check_claims(owner, repo, a.number)

    if a.cmd == 'status':
        # One payload renders the digest and decides the code, for the reason `wait` reads one.
        # Fetched twice, a round landing between them prints one pull request and grades another.
        pr = gql(Q_FULL, owner, repo, a.number)
        out, _ = digest(owner, repo, a.number, pr=pr)
        print(out)
        return report_verdict(pr)

    if a.cmd == 'reply':
        return reply_to_thread(owner, repo, a.number, a.match, a.body, a.path, a.resolve)

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
        # Coverage of the head is what this returns 0 on.
        # Coverage of the diff is a second reading it used to take on trust.
        # A round can carry the head and have read only part of it.
        return report_verdict(final)
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
