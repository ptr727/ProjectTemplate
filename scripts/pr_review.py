#!/usr/bin/env python3
"""Consolidated Copilot-review status for a PR - one command, one compact digest.

Why this exists: 4,513 of the measured `gh` invocations were one call per agent turn, and
each turn re-bills the whole session context. The bytes `gh` returns are trivial, averaging
574, so the round-trips are the entire cost. This collapses a poll cycle into a single
invocation whose output is a few hundred bytes. See GOVERNANCE.md "Context and Delegation
Discipline" for the rule this implements.

Subcommands
  comment  Post one PR-conversation answer, including a suppressed-finding disposition. The PR
           node id is read in the same run, and the returned comment URL and body confirm the
           write. Exit 0 = done, 64 = write scope could not be established or excludes the
           target, 65 = the PR could not be read, 66 = the response did not confirm the comment.
  claims   Check the description against the branch it describes. A body claiming a commit or
           quoting a `uses:` ref the head tree no longer carries is a silent failure caught by a
           reviewer or not at all, and three stale descriptions in one session generated six
           review findings between them. Read-only. Exit 0 = every reference resolves, 70 = one
           does not, 71 = there were references and none could be read, so nothing was decided.
  status   One digest line, any unresolved threads, and any suppressed findings. Read-only.
           Exit 0 = no Copilot review covers the head yet, or every output shape is recognized
           and the round covering the head read the whole diff. `review_on_head` and `rounds=`
           in the digest name Copilot's own coverage specifically, the reviewer this script
           requests and waits for, never "no review of any kind covers this head": a
           tracked other reviewer, named under `other_reviewed` below, can carry the exact head
           commit while Copilot's own `review_on_head` still reads `NO`, and an empty body from
           that other reviewer on that head is its own ordinary "reviewed, nothing to flag"
           shape, the same reading an empty-bodied Copilot round already gets, not a gap.
           Use `wait` when review presence is the condition, since `status` reports an absent
           review without treating it as a failure.
           42 = that round read fewer files than the pull request changed, so part of the diff
           has no review at all. Measured over four pull requests and seven rounds here, a
           re-request never cleared one and no round ever recovered, so this is a state to
           hand to the maintainer rather than one to retry into.
           43 = the reviewer sent a shape this script has no reader for, so no field here can
           be believed. The remedy is an issue on the repository hosting this script, and the
           review loop does not close until the reader is fixed. Merging regardless is the
           maintainer's decision rather than the agent's.
           45 = the review covering the head stated no changed-file coverage. Request another
           review only after confirming the head branch carries the current review instructions.
           A refusal naming the account quota still reads as absent here, exit 0, since a
           refusal covers no head either. Its printed digest line carries `refusal=QUOTA`
           regardless. `wait` is where that state gets its own exit codes, 46 and 47 below,
           because only `wait` is the command a caller might otherwise poll out a timeout on.
           `unresolved` counts every tracked reviewer's own open thread, not only Copilot's:
           CodeRabbit (`coderabbitai`) and qodo (`qodo-code-review`) are tracked at the identity
           and thread-resolution level, since an open thread blocks a ruleset-gated merge
           whoever opened it and `unresolved=0` once hid one of theirs that still did (PR #915).
           Both `threads=` and `unresolved=` are read from a single 100-thread page with no
           further pagination, so a pull request carrying more than that undercounts silently
           past that point: both fields print a trailing `+` and a `THREADS TRUNCATED` block
           follows, naming the gap rather than leaving either count to be trusted as whole
           (#973). Reading past the cut needs `reply`'s own paginated walk instead. `suppressed=`
           and `cr_outside_diff=` carry the identical marker over the separate 100-review page
           they are both read from, a `REVIEWS TRUNCATED` block following, since an unanswered
           finding on a round old enough to fall out of that window is exactly as invisible.
           Coverage, refusal, and `wait`/request support stay Copilot-only, each being its own
           format and its own future task per bot. Where either has posted anything at all on
           the current head, `other_reviewed` names it, identity and commit only, no verdict
           read from what it said. `other_rate_limited` names one whose newest comment or review
           carries its own rate-limit marker instead, a structural
           `<!-- ... rate limited by ... -->` convention rather than free-text prose (observed on
           CodeRabbit, ptr727/Blog #110), so reading it needs no per-bot wording model the way
           Copilot's quota refusal needed `QUOTA` built for its own text.
           Two of their own finding shapes are read, though, each its own blind spot a thread
           poll alone cannot see. `cr_outside_diff=N (on_head=X earlier=Y)` counts
           CodeRabbit's own "outside diff range" findings, collapsed into the review body rather
           than raised as an inline review comment because the finding sits on a line outside
           the pull request's changed hunks. Printed once CodeRabbit has raised one, on any
           round, or once the reviews window is truncated, since an older round could then
           still carry one unseen (`cr_outside_diff=0+`). Stays silent otherwise, whether that
           silence means no finding was raised or CodeRabbit was never trialed at all.
           `qodo_open=N` counts Qodo's own
           numbered findings that carry neither its `Resolved` nor `Dismissed` self-tracked
           badge: Qodo's formal review carries an empty body on every round observed, so its
           findings are read from its "Code Review by Qodo" PR-level comment instead, not
           head-scoped since a comment carries no commit. Printed as `0` once Qodo has posted
           that comment at all, since a `0` is itself a reading, `unknown` where its findings
           comment specifically could be sitting behind the 100-comment window even while a
           paired `PR Summary by Qodo` stays visible, so its silence cannot be told from it
           never having commented, and absent only where it genuinely never has. Qodo's own
           badge is a fast pre-triage signal, not a substitute for reading the finding:
           spot-verify against `gh pr diff` rather than trusting it outright.
  reply    Answer one thread selected by its text, and resolve it on request. Exists
           because the hand-run form keeps failing the same way: a node id typed into a
           mutation, which resolves globally and so writes to a real thread somewhere
           rather than failing. This takes a pull request number and words from the
           finding, queries the id itself, and offers no argument an id fits in. Exit 0 =
           done, 60 = no thread matched, 61 = more than one did, 62 = the reply returned
           no comment url so nothing was resolved, 63 = the resolve did not report the
           thread resolved, 64 = the target is under another owner.
  wait     Request a review where none is outstanding, then poll until Copilot's review lands
           on the current head, then print the digest. The auto-request is skipped once a
           review already covers the head, once Copilot has already answered outside a formal
           review, or once one is already in the pending request set, so calling `wait` again on
           the same PR never double-requests. It reads the Copilot reviewer's bot id from the
           repository's own most recently updated PRs rather than a fixed id: the last
           HISTORY_PRS, widened once to HISTORY_PRS_WIDE where that narrow window carries no
           Copilot activity at all, since an outage that outlasts HISTORY_PRS PRs would otherwise
           empty it on every call for as long as the outage runs (#985). Requests nothing
           (falling back to polling only) where both windows come up empty, since a repository
           with no Copilot review in either has nothing to read the id from and a fabricated one
           is never an option. The loop runs in-process, so a 45-minute wait costs one agent
           turn, not 90.
           Exit 0 = review present, 30 = still pending at timeout (pending is not failure),
           40 = Copilot answered outside a formal review, so read the printed body.
           40 reports the shape of that answer and reads nothing of its cause: an answer
           carrying no commit covers no head, so the wait ends and the reader decides.
           41 = the review carrying the head says it did not review, so it covers nothing.
           42, 43, and 45 = the review landed and `status`'s blocking readings apply to it,
           since a wait ending on a round that covered half the diff, or on output nothing here
           can read, has ended on something other than a review of this pull request.
           44 = the review loop closed, the merge reads BLOCKED, and a check is in a shape no
           wait clears: queued with nothing acting on it, expected and never posted, running
           far past what the job costs, or failed. A check merely still running normally is
           not this and exits 0, and neither is a stuck check on a merge that is not BLOCKED,
           since the rollup carries checks no ruleset requires. The digest reports the check
           in both cases, so a shape outside 44 is still named rather than lost.
           46 = the review carrying the head is a refusal naming the account quota specifically,
           printed above under COPILOT REFUSED THIS ROUND. That is an account-level state a
           re-request or a further wait does not clear, unlike 41's other causes (a file count
           over the limit, cleared by splitting the pull request), so it is its own code rather
           than folded into 41: proceed on the other reviewers' coverage instead of retrying.
           47 = this pull request's current head carries no Copilot activity of its own, and
           the reviewer's own most recent activity found in the repository, a review or comment
           on any pull request including an earlier round on this one, is that same
           account-quota refusal with nothing having answered it since. The poll that would
           otherwise have run is skipped for this reason, printed as a `note:` line before the
           digest, rather than spent finding the same account state out a call late. Pass
           --ignore-quota-signal to poll --timeout anyway once the quota is believed to have
           reset. 46 is read directly from the current head and always takes priority over 47,
           so a genuine 0/40/41/42/43/45 on this pull request outranks 47 whenever both
           would otherwise apply.
           A pending request remains pending until a review, an answer, or the timeout. GitHub's
           effort-labeled review lifecycle does not always emit `copilot_work_started`, so that
           event is not evidence that distinguishes queued work from abandoned work.

Reading is the bulk of this and the writing commands are a trade rather than a free win. A
mutation spelled as a `gh` command in a shell is read by the gh-write-guard PreToolUse hook, and
one this script performs is not. The script removes the guarded failure at its source: every node
id comes from a live query in the same run, and the owner boundary is enforced in-process. See
.github/copilot-instructions.md for the runbook, and GOVERNANCE.md "Repository Boundaries and
Write Safety" for the rules these commands enforce.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

REVIEWER = "copilot-pull-request-reviewer"
# Other review bots this repository has trialed alongside Copilot.
# Tracked at the identity level only, login and commit oid, never body prose, except where a reader below names one explicitly.
# Each format read here is its own reader, and doing that well is a separate task per bot.
# What generalizes without reading any of their prose is thread resolution.
# An open thread blocks a ruleset-gated merge whoever opened it, and `status`'s `unresolved=0` once silently hid a CodeRabbit/qodo thread that did block one (PR #915, ptr727/ProjectTemplate).
# Login spellings are read off this repository's own history (`gh pr view --json reviews,comments`) rather than guessed.
CODERABBIT_LOGIN = "coderabbitai"
QODO_LOGIN = "qodo-code-review"
# Named rather than inlined at each of their own readers below, so a login rename updates one spelling instead of silently leaving a hardcoded copy matching nothing.
OTHER_REVIEWERS = (CODERABBIT_LOGIN, QODO_LOGIN)
KNOWN_REVIEWERS = (REVIEWER, *OTHER_REVIEWERS)

# A check dispatched to a runner and not begun, in the spellings a CheckRun carries.
# PENDING here means dispatched and not begun, which a StatusContext's means the opposite of.
# That one is translated away in check_nodes, where the node shape is still known.
# Reading QUEUED alone scores the rest of these as running.
NOT_STARTED = frozenset({"QUEUED", "WAITING", "PENDING", "REQUESTED"})
# A required status nothing has posted, which is a StatusContext's state and only ever that.
# It is not a starved job and must not borrow that remedy, since no runner is owed it at all.
# Left out of both sets it reaches the conclusion branch, where it reports as a red check.
NOT_POSTED = frozenset({"EXPECTED"})
# What counts as a check having passed, rather than as one still deciding or failed.
# SKIPPED and NEUTRAL are passes, since the fleet aggregator pattern skips the conditional jobs.
# Treating a skip as unfinished reports four blockers on every green pull request here.
CHECK_OK = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
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
SUPPRESSED = re.compile(r"Suppressed comments|low confidence", re.IGNORECASE)
# CodeRabbit's own equivalent, collapsed into the review body like `SUPPRESSED` rather than raised as an inline comment.
CR_OUTSIDE_DIFF = re.compile(r"Outside diff range comments", re.IGNORECASE)
# A refusal declines the round as a formal review carrying the head and no threads.
# That is the clean pass byte for byte, so every coverage check passes over a round that never ran.
# The alternation is the runbook's for the same reason the one above is.
# One phrasing is one rewording away from reading a refusal as a review.
# The dot covers the apostrophe in the typographic spelling and the ASCII one alike.
# It also keeps the published filter usable inside single quotes, which neither survives.
REFUSAL = re.compile(
    r"wasn.t able to review|was not able to review|unable to review", re.IGNORECASE
)
# The account-level cause among the refusal wordings, as opposed to a per-pull-request one like the file count.
# Read against `refusal_of`'s own return rather than the raw body.
# That way the exemptions built for that anchor, the opening line and the fenced quotation, protect this reading too instead of needing their own.
# This script's own corpus and this file both quote the sentence below its overview, same as the refusal wording itself does.
# Observed once, on PR #962 here: "Copilot was unable to review this pull request because the user who requested the review has reached their quota limit."
QUOTA = re.compile(r"reached (?:their|its|his|her|your|my) quota limit", re.IGNORECASE)
# A structural marker rather than prose, so reading it needs no per-bot wording model the way `QUOTA` above needs one for Copilot's free-text refusal.
# Observed on CodeRabbit, a plain PR comment rather than a formal review, ptr727/Blog #110: "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->".
# The service name is captured rather than assumed.
# A second bot using the same auto-generated-comment convention is read without a new pattern, and one that does not use it stays unread rather than guessed at.
RATE_LIMITED = re.compile(r"auto-generated comment:\s*rate limited by\s*(\S+?)\s*-->")
# Anchored to the `<h3>` tag rather than a bare substring, since prose elsewhere can mention the phrase without being the comment it names.
QODO_REVIEW_HEADING = re.compile(r"<h3>\s*Code Review by Qodo\s*</h3>", re.IGNORECASE)
# Only the numbered heading counts as a finding, told apart from Qodo's own nested sub-summaries by starting with a number and a period (`1.`, `2.`, ...).
QODO_FINDING = re.compile(r"\s*\d+\.\s")
# A finding's own title can quote `Resolved`/`Dismissed` without carrying the badge, so the glyph is required rather than just the word.
# Escaped (U+2713, U+2717) rather than typed literally, per the repository's ASCII charset rule.
QODO_BADGE = re.compile(r"<code>[^<]*(?:\u2713 Resolved|\u2717 Dismissed)[^<]*</code>")
# A round states how much of the diff it read on a line of its own.
# A round that read part of it is the clean pass elsewhere, same commit and threads and digest.
# Five such rounds landed across three merged pull requests here.
# One of them read 2 of 3 changed files across both its rounds and merged.
# This is the third instance of the shape the two patterns above answer.
# It is also the only one nothing was reading.
# The line is anchored at its start rather than matched body-wide, both spellings being structural.
# Over 333 review bodies every coverage statement opens its line and not one sits mid-sentence.
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
COVERAGE_BULLET = re.compile(r"\s*[-*]\s*\*\*Files reviewed:", re.IGNORECASE)
COVERAGE_SENTENCE = re.compile(r"\s*Copilot\b", re.IGNORECASE)
# Ask the reviewer for a stable coverage shape instead of adapting only to changing prose.
# Keep the prose readers for reviews made before the marker shipped.
FLEET_REVIEW = re.compile(
    r"\s*<!--\s*fleet-review:\s*reviewed=(\d+)\s+changed=(\d+)\s+findings=(\d+)\s*-->\s*",
    re.IGNORECASE,
)
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
    r"reviewed\s+(\d+)\s+out of\s+(\d+)\s+changed files?"
    r"|\*\*Files reviewed:\*\*\s*(\d+)\s*/\s*(\d+)(?:\s+changed files?)?",
    re.IGNORECASE,
)
# A fenced block is a quotation rather than a statement, and 131 of those bodies carry one.
# This change puts both spellings into the source and the runbook, so a review of it quotes them.
# A quoted count read as this round's own is a coverage figure nobody stated.
FENCE = re.compile(r"^ {0,3}```.*?^ {0,3}```[^\n]*", re.DOTALL | re.MULTILINE)
# An inline code span is a quotation for the same reason a fenced block is.
# A reviewer naming `<summary>` in prose was read as opening one.
# That swallowed the body from there to the next real close tag.
# Matched longest-run-first so a double-backtick span carrying a backtick closes on its own run.
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)", re.DOTALL)
# The round's own file summary table, whose first column is a path and whose second is prose.
# The header row is the marker rather than the `<details>` around it.
# One measured round carries the table with no `<summary>`, and two summary spellings wrap it.
# Over the same 348 bodies, 91 carry a table and every table row in the corpus belongs to one.
# A row outside this shape is a shape nothing has seen rather than a table read wrongly.
TABLE_HEADER = re.compile(r"\s*\|\s*File\s*\|", re.IGNORECASE)
# The alignment row under the header, which is punctuation rather than a file.
TABLE_RULE = re.compile(r"\s*\|[\s:|-]+\|\s*$")
TABLE_ROW = re.compile(r"\s*\|([^|]*)\|")
# The readings a round's coverage carries, worst first.
# A head carries more than one round only through a re-request.
# Where two disagree, the one naming files it did not read is the one to answer.
# `UNSTATED` sits last rather than beside the failures, being the absence of a statement.
# A round that did state full coverage settles the question over one that stated nothing.
UNVETTED, PARTIAL, FULL, UNSTATED = "unvetted", "partial", "full", "unstated"
SEVERITY = (UNVETTED, PARTIAL, FULL, UNSTATED)
# Upper-case for the two that block a merge, for the reason `review_on_head=NO` is upper-case.
# `unstated` rather than `unknown`, since a body carrying no count is a shape this knows.
# What this script does not know is the separate `shapes` field, and one word for both hides it.
# The constant carries that name too, so no reader has to map a name here onto another word.
COVERAGE_FIELD = {UNVETTED: "UNVETTED", PARTIAL: "PARTIAL", FULL: "full", UNSTATED: "unstated"}

# Every structural marker the reviewer's own bodies carry, measured over the same 333.
# A body is read for these rather than trusted, because every reader below keys on one of them.
# A heading this script has no spelling for is a section it will not find, reported as absent.
# That is the shape of all three failures already on record here, each caught after it landed.
# The lists are small because the output is regular: 9 headings, 6 summaries and 3 labels.
# Counts are normalized to `(N)` and non-ASCII is dropped before comparing, and `unvetted` folds letter case at the comparison itself.
# The verdict headings carry a colored circle, so the emoji is what would drift most cheaply.
# Dropping it also keeps this file inside the charset rule that governs the repository.
VETTED_HEADINGS = {
    "## Pull request overview",
    "### Reviewed changes",
    "### Approval recommended",
    "### Ready to approve",
    "### Changes recommended",
    "### Needs a closer look",
    "### Not ready to approve",
    "### Human review recommended",
    "### Suppressed comments (N)",
}
VETTED_SUMMARIES = {
    "Pull request overview",
    "Show a summary per file",
    "File summaries",
    "Review details",
    "Suppressed comments (N)",
    "Comments suppressed due to low confidence (N)",
}
VETTED_LABELS = {"Files reviewed", "Comments generated", "Review effort level"}
MARKDOWN_HEADING = re.compile(r"\s*#{1,6}\s")
# The `Review details` metadata bullets, of which the coverage line is one.
LABEL_LINE = re.compile(r"\s*[-*]\s+\*\*([^*]+):\*\*")
EFFORT_LINE = re.compile(
    r"\s*[-*]\s+\*\*Review effort level:\*\*\s*"
    r"(?:(Default)\s*\(\s*(Lite|Balanced|Max)\s*\)|(Lite|Balanced|Max))\s*$",
    re.IGNORECASE,
)
# A login that reads as this reviewer without being the spelling every query here filters on.
# A rename leaves every filter matching nothing, so a review that landed reads as none at all.
# A wait then polls out its whole timeout against a review sitting in plain sight.
# `copilot-swe-agent` is the coding agent rather than the reviewer, and does not match this.
READS_AS_REVIEWER = re.compile(r"copilot.*review", re.IGNORECASE)
# The repository this script is hosted in, named because an unrecognized shape is fixed here.
# It is a literal rather than the pull request's own repository.
# That one is where the shape was seen, not where the reader failing on it lives.
HUB = "ptr727/ProjectTemplate"
SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.DOTALL | re.IGNORECASE)
TAGS = re.compile(r"</?(?:details|summary)>", re.IGNORECASE)
COUNT = re.compile(r"\((\d+)\)")
# What makes a line a heading rather than prose mentioning the phrase, in either markup.
# A line that is neither still qualifies where it carries a count.
# The section has appeared as a bare line too, and a count is what prose does not carry.
HEADING = re.compile(r"\s*(?:#{1,6}\s|<summary)", re.IGNORECASE)

# How many of the newest reviews and comments both queries read.
# A narrow window drops the reviewer's answer behind ordinary discussion, reporting no answer.
# A test holds this equal to the number the queries carry, since a drift between them reads clean.
WINDOW = 100

# How many of the pull request's own changed files the full query asks for.
# 100 is the connection's own ceiling, so a longer diff is truncated rather than paged.
# A truncated list is reported as one and compared against nothing.
# A path missing from a short window reads exactly like a path the reviewer left out.
# The record holds a pull request of 301 changed files, so the truncation is reachable here.
FILES_WINDOW = 100

# How many rollup contexts the full query asks for, which is not the window above.
# The query is built from this and the truncation line quotes it, so the two cannot drift.
# Naming it beside a hard-coded literal only documented the literal, which a case then held.
# A case is not the guarantee though, since it holds only where someone runs it.
CHECKS_WINDOW = 100

# How many of the repository's own pull requests, and how much of each one's own reviewer history within them, the repo-wide history read looks across.
# Smaller than WINDOW on purpose, since this is a best-effort signal rather than the certainty WINDOW's own two queries are read for.
# Neither carries a `hasPreviousPage` guard for that same reason.
# A caller finding the signal wrong once quota has plainly reset already has --ignore-quota-signal for it, which is a cheaper remedy than paginating this window out.
HISTORY_PRS = 20
HISTORY_REVIEWS = 20
HISTORY_COMMENTS = 5

# The one-time widened retry `copilot_history` reaches for where HISTORY_PRS carries no Copilot activity at all, rather than reverting every caller to blind polling for the rest of an outage that outlasts it (#985).
# GitHub's own connection ceiling for a single `first`, the same reason FILES_WINDOW and CHECKS_WINDOW hold it.
# Reaching further back needs cursor pagination, which this best-effort signal is not worth paying for, so one wider try is where this stops.
HISTORY_PRS_WIDE = 100

# Liveness query: timestamps and ids only, no comment or review bodies.
# A liveness check does not need the finding text, and re-fetching bodies was 76% of polls.
# It does need the reviewer's non-review answers.
# A wait reading formal reviews alone treats a refusal as an unmet condition.
Q_LIVE = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    id headRefOid
    reviews(last:100){ nodes{ author{login} state commit{oid} submittedAt } pageInfo{ hasPreviousPage } }
    comments(last:100){ nodes{ author{login} createdAt } pageInfo{ hasPreviousPage } }
    reviewRequests(first:10){ nodes{ requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } }
  }}}
"""

# The reviewer's own review and comment history, read live from the repository's own pull requests rather than hand-typed or cached across runs.
# Two callers share this one query.
# The bot's node id belongs to the reviewer account rather than to any one PR or review, so it is identical wherever it is found and recency does not matter once one is found.
# A repo-wide reading of the reviewer's own most recent activity is the other caller, and recency is the whole point there, so `submittedAt`, `createdAt`, and `body` ride along for a reader that does not stop at the first bot id.
# Ordered by `UPDATED_AT` rather than `CREATED_AT`, since a fresh review round bumps a pull request's own update time regardless of how long ago it was opened, where creation order can leave a re-reviewed older pull request outside the window entirely.
# `reviews(last:$reviews)` reads the newest rounds a pull request carries rather than the oldest, and `comments(last:$comments)` catches a plain answer that supersedes a formal review without needing every comment a busy pull request holds.
# It resolves even on a pull request whose own round 1 carries no review yet, per the fleet's standing rule against fabricating or reusing a GitHub node id.
# `$prs` is a variable rather than baked into the document, unlike the other windows in this file, because `copilot_history` runs this same query twice on an empty narrow read, once at HISTORY_PRS and, only then, once at HISTORY_PRS_WIDE (#985).
# One document read at two widths costs nothing a second document would not, and it keeps the two reads provably identical apart from that one number.
Q_BOT_ID = """
query($o:String!,$r:String!,$prs:Int!,$reviews:Int!,$comments:Int!){
  repository(owner:$o,name:$r){
    pullRequests(first:$prs, orderBy:{field:UPDATED_AT, direction:DESC}){
      nodes{ number
        reviews(last:$reviews){ nodes{ author{ __typename login ... on Bot{ id } } state body submittedAt } }
        comments(last:$comments){ nodes{ author{ login } body createdAt } }
      } } } }
"""

M_REQUEST_REVIEWS = """
mutation($pr:ID!,$bot:ID!){
  requestReviews(input:{pullRequestId:$pr, botIds:[$bot], union:true}){ pullRequest{ id } }}
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
      comments(first:1){ nodes{ author{login} path line body } } } pageInfo{ hasNextPage } }
    comments(last:100){ nodes{ author{login} createdAt body } pageInfo{ hasPreviousPage } }
    reviewRequests(first:10){ nodes{ requestedReviewer{ __typename ... on Bot{login} ... on User{login} } } }
    files(first:__FILES_WINDOW__){ pageInfo{ hasNextPage } nodes{ path } }
    commits(last:1){ nodes{ commit{ oid statusCheckRollup{ state
      contexts(first:__CHECKS_WINDOW__){ pageInfo{ hasNextPage } nodes{
        __typename
        ... on CheckRun{ name status conclusion startedAt }
        ... on StatusContext{ context state createdAt }
      }}}}}}
  }}}
""".replace("__CHECKS_WINDOW__", str(CHECKS_WINDOW)).replace("__FILES_WINDOW__", str(FILES_WINDOW))
# Substituted rather than interpolated, because GraphQL is braces from end to end.
# An f-string would need every one of them doubled, which is unreadable against the schema.


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
    r"\b(?:fixed|landed|shipped|added|introduced|corrected|resolved|carried|amended)"
    r"\s+(?:in|by|as)\s+`?(?P<sha>[0-9a-f]{7,40})`?",
    re.IGNORECASE,
)
# What `gh` prints when GitHub answered, as opposed to when nothing was reached at all.
HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
# The two GitHub returns for an object that is not there.
# A network error carries no status, and 401 and 403 are credentials and a rate limit.
# Reading one of those as absence reports a correct description as stale.
# Everything outside this set is therefore "not read" rather than "not there".
ABSENT = {"404", "422"}
# What a reference reads as where GitHub never answered for it.
UNREAD = "unread"
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

# The PR id for a conversation comment is read in the same run that writes it.
Q_COMMENT_TARGET = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ id url } }}
"""

# The conversation-comment and thread mutations the runbook publishes.
# `url` is fetched because it is the one field that confirms a comment or reply carried a body.
# A reply that posted empty still returns a comment, and three did, each then resolved.
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
    """Run one GraphQL document and return its `data`, raising rather than reporting a blank.

    A string goes through `-f` and an int through `-F`, because `-F` infers a type from the text:
    a reply body of `123` or `true` arrives as an Int or a Boolean and the mutation fails on a
    type nobody passed it, and a body opening with `@` is read as a filename.

    `errors` is checked rather than trusted to the exit code, since a GraphQL document can fail
    per-field while the request itself succeeds, and the caller would read the null that leaves.
    """
    # Every read below decodes as UTF-8 rather than as whatever the platform's locale is.
    # `gh` emits UTF-8 on every platform, where a Windows console locale is cp1252.
    # A review body carrying one typographic quote crashed the decode and left the caller reading a null stdout.
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        argv += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", check=False)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[:800])
        raise SystemExit(f"gh graphql failed rc={r.returncode}")
    payload = json.loads(r.stdout)
    if payload.get("errors"):
        sys.stderr.write(json.dumps(payload["errors"])[:800])
        raise SystemExit("gh graphql reported errors")
    return payload["data"]


def gql(query: str, owner: str, repo: str, num: int) -> dict:
    return gh_graphql(query, o=owner, r=repo, n=num)["repository"]["pullRequest"]


def reviewer_requested(pr: dict) -> bool:
    """True where the reviewer sits in the pending request set.

    Read from GraphQL rather than `gh pr view --json reviewRequests`, which omits a Bot
    reviewer entirely and reports an empty set while the reviewer is sitting in it.
    """
    return any(
        (n.get("requestedReviewer") or {}).get("login") == REVIEWER
        for n in ((pr.get("reviewRequests") or {}).get("nodes") or [])
    )


def copilot_history(owner: str, repo: str) -> list[tuple[int, dict]]:
    """The reviewer's own reviews and comments across the repository's most recently updated
    pull requests, newest activity first regardless of which connection it came from.

    Read at HISTORY_PRS first and, only where that comes back carrying no usable bot id, read
    again at the wider HISTORY_PRS_WIDE. A narrow window emptying out is the ordinary case, a
    repository whose most recent activity genuinely carries none of the reviewer's, and costs
    nothing beyond the one call either caller below was always going to make. It stops being
    ordinary once an outage outlasts HISTORY_PRS pull requests: every one of them then carries
    the same silence, the narrow window empties out too, and both callers would otherwise fall
    back to blind polling for the rest of the outage with no way to tell that outage apart from a
    repository that has simply never seen a Copilot review (#985, reproduced on
    ptr727/ProjectTemplate PRs #981-984). The wider read is what tells the two apart.

    Emptying out is not the only way the narrow window fails a bot-id lookup, though: it can
    carry real activity and still have none, when every entry within it is a plain comment. A
    formal review, `copilot_bot_id`'s only source for the id, can sit just outside the narrow
    window while a newer comment sits inside it, and returning the narrow read as soon as it has
    anything at all left that review permanently unread. Widening is keyed on
    `copilot_bot_id(entries)` rather than on emptiness for exactly that case, so a comment-only
    narrow window still triggers the wider read the same way an empty one does. The ordinary case
    -- a narrow window already carrying a review -- still costs one call rather than two, since
    that is the common shape a usable bot id already satisfies.
    """
    entries = _copilot_history_window(owner, repo, HISTORY_PRS)
    if entries and copilot_bot_id(entries) is not None:
        return entries
    return _copilot_history_window(owner, repo, HISTORY_PRS_WIDE)


def _copilot_history_window(owner: str, repo: str, prs: int) -> list[tuple[int, dict]]:
    """One read of `copilot_history`'s traversal, over the `prs` most recently updated pull
    requests. Split out so the narrow and widened reads share every line but the one count.

    Paired with the pull request number each came from, since a repo-wide reading has to name
    where it looked rather than just assert one. Sorted by timestamp rather than trusted to
    arrive in that order, since the two connections are read separately and merged.

    Unfiltered, including the caller's own pull request if it appears in the window. No pull
    request is special-cased out: `copilot_bot_id` is happy to read a valid id from anywhere,
    including an earlier round on the very pull request `wait` is running against, and
    `quota_signal` treats an earlier round on that same pull request as real evidence about the
    account rather than a self-reference to discard. A refusal on the caller's own current head
    is caught directly, at higher priority, before either reading here is ever consulted.

    A plain comment is read alongside a formal review, tagged `_kind` so `copilot_bot_id` can
    still tell them apart, because `answered_outside_review` already treats a comment as
    meaningful reviewer activity for one pull request, and a repo-wide reading that only checked
    reviews could keep reporting a stale refusal after a newer plain comment answered it.

    One call serves both the bot-id lookup and the repo-wide quota reading below, since both
    need the same traversal and a caller needing either otherwise pays for it twice.
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
                entries.append(
                    (number, {**review, "_at": review.get("submittedAt") or "", "_kind": "review"})
                )
        for comment in (node.get("comments") or {}).get("nodes") or []:
            if (comment.get("author") or {}).get("login") == REVIEWER:
                entries.append(
                    (number, {**comment, "_at": comment.get("createdAt") or "", "_kind": "comment"})
                )
    return sorted(entries, key=lambda pair: pair[1]["_at"], reverse=True)


def copilot_bot_id(history: list[tuple[int, dict]]) -> str | None:
    """The Copilot reviewer bot's node id from an already-fetched history, or None where none
    carries one.

    Read from the review entries only, since a comment's author carries no node id in the query
    that fetches it, that field belonging to the review connection's `... on Bot{ id }` selection.
    None is a real answer, not a failure to retry: a repository's very first PR, or one Copilot
    has never reviewed, carries no review to read the id from. The caller falls back to polling
    only rather than guessing, since a fabricated or reused id resolves globally and a wrong one
    would silently target another repository's PR.
    """
    for _number, entry in history:
        if entry.get("_kind") != "review":
            continue
        bot_id = (entry.get("author") or {}).get("id")
        if bot_id:
            return bot_id
    return None


def quota_signal(history: list[tuple[int, dict]]) -> tuple[int, dict] | None:
    """The reviewer's own most recent activity anywhere in this repository, review or comment,
    where it is a quota refusal, paired with the pull request number it came from.

    The most recent rather than any match, since a repository can carry an old refusal and a
    later working round both, and only the most recent record says which is still true. A
    genuine review anywhere since the refusal spends it, the same reading `refusing_review`
    already gives one pull request's own history, and so does a later plain comment, since
    `answered_outside_review` treats one the same way for that one pull request. Reading across
    pull requests rather than one is what a repository-wide account state needs, since the pull
    request in front of a caller can carry none of its own reviewer activity to read that state
    from at all.
    """
    newest = history[0] if history else None
    return newest if newest and quota_refusal(newest[1]) else None


def rate_limited_by(pr: dict, login: str) -> str | None:
    """The service name a rate-limit marker gives, where this login's newest comment or review
    on this pull request carries one. `None` otherwise.

    Reads both connections since the one observed marker (CodeRabbit, ptr727/Blog #110) rides a
    plain PR comment rather than a formal review, and a future bot using the same convention
    might use either. Current pull request only, unlike Copilot's `quota_signal`: that one
    generalizes from a pattern of silence across several pull requests, and this has one
    observed instance to generalize from, not a history to read one out of.
    """
    nodes = reviewer_nodes(pr, "comments", login) + reviewer_nodes(pr, "reviews", login)
    if not nodes:
        return None
    newest = max(nodes, key=lambda n: n.get("createdAt") or n.get("submittedAt") or "")
    match = RATE_LIMITED.search(newest.get("body") or "")
    return match.group(1) if match else None


def request_copilot_review(pr_node_id: str, bot_id: str | None) -> str:
    """Ask Copilot to review the current head, and say in one line what happened.

    This exists because `wait` used to only ever poll, never request, so a PR whose auto-seed
    never fired or whose prior request was superseded by a later push sat waiting the full
    timeout for a review nothing had asked for, twice in one session before this was written.
    `union:true` adds the bot to the request set rather than replacing it, so a human reviewer
    requested alongside it stays requested. Raises where `gh_graphql` does, on the mutation
    itself, the same as every other write in this script: a failed write is reported, never
    quietly swallowed into a fallback that would recreate the exact blind-poll failure this
    function exists to prevent, from a different cause. The one quiet path is finding no bot id
    to request with at all, which is not a failure, since a repository with no Copilot review
    anywhere carries nothing to read the id from. The id itself is the caller's to find, via
    `copilot_bot_id` over a `copilot_history` read it already paid for, which already tried both
    the narrow HISTORY_PRS window and the wider HISTORY_PRS_WIDE one before coming up empty (#985).
    """
    if not bot_id:
        return (
            f"no Copilot review found across the last {HISTORY_PRS} or, widened once for "
            f"exactly this reason, the last {HISTORY_PRS_WIDE} most-recently-updated pull "
            "requests to read the reviewer bot id from, so nothing was requested here, "
            "polling only. Seed one via the UI if this repository has never had one at all."
        )
    gh_graphql(M_REQUEST_REVIEWS, pr=pr_node_id, bot=bot_id)
    return f"requested a Copilot review on the current head (bot {bot_id})"


def reviewer_nodes(pr: dict, field: str, login: str = REVIEWER) -> list[dict]:
    """This login's own nodes under `field`, oldest first as the API returns them.

    `login` defaults to the fully-modeled reviewer, so every existing call keeps reading
    Copilot's own nodes unchanged. A caller reading another known reviewer's presence
    generically, identity and commit only, no body parsing, passes it explicitly.
    """
    return [
        n
        for n in ((pr.get(field) or {}).get("nodes") or [])
        if (n.get("author") or {}).get("login") == login
    ]


def answered_outside_review(pr: dict) -> dict | None:
    """The reviewer's newest plain comment, where it postdates its newest formal review.

    The test is the shape of the answer rather than its cause, which this reads nothing of:
    a comment carries no commit, so it satisfies no coverage check whatever it says.
    Treating that shape as an unmet condition is what leaves a wait with nothing at its end.
    A comment older than the newest review is spent, since the review it preceded did land.
    """
    comments = reviewer_nodes(pr, "comments")
    # A blind review window leaves no honest baseline to date a comment against.
    # An empty one dates every comment as newer, so each reads as an answer.
    # Reporting nothing keeps the wait polling, where a wrong answer ends it outright.
    if not comments or window_blind(pr, "reviews"):
        return None
    newest = max(comments, key=lambda n: n.get("createdAt") or "")
    reviews = reviewer_nodes(pr, "reviews")
    latest_review = max((n.get("submittedAt") or "" for n in reviews), default="")
    return newest if (newest.get("createdAt") or "") > latest_review else None


def window_blind(pr: dict, field: str, login: str = REVIEWER) -> bool:
    """True where this login's own nodes can sit behind the window, so the view cannot decide.

    `login` defaults to the fully-modeled reviewer, so every existing caller keeps reading
    Copilot's own blind spot unchanged. A caller reading another known reviewer's own window,
    Qodo's `comments` connection carrying its findings comment, passes it explicitly.

    Each query reads the newest nodes rather than this login's, so ordinary traffic is what
    pushes theirs out of reach. Nodes arrive in creation order, so anything behind the window is
    older than everything inside it: one of this login's in view bounds every hidden one as
    older still, which settles the question rather than leaving it open. `hasPreviousPage` is
    what says anything is back there at all, since a full window and a window holding the lot
    are the same length.
    """
    older = ((pr.get(field) or {}).get("pageInfo") or {}).get("hasPreviousPage")
    return bool(older) and not reviewer_nodes(pr, field, login)


def threads_truncated(pr: dict) -> bool:
    """True where `reviewThreads(first:100)` cut off before this pull request's actual thread
    count, so `threads=`/`unresolved=` below undercount rather than reading the true total.

    Unlike `window_blind`'s `last`-windowed connections, `reviewThreads` reads forward from the
    first page in the connection's own creation order, so `hasNextPage` here means the threads
    left unread are the newest ones, not the oldest, exactly the ones most likely to still be
    open. That inversion is why this is its own guard rather than a second use of `window_blind`:
    that one settles the question from what is already in view, and there is no such settling
    available here, only the fact that something was cut (#973, undercounted since PR #969
    widened `unresolved` from Copilot's own threads to every tracked reviewer's).
    """
    return bool(((pr.get("reviewThreads") or {}).get("pageInfo") or {}).get("hasNextPage"))


def reviews_truncated(pr: dict) -> bool:
    """True where the `reviews` connection's own page cut off before this pull request's actual
    round count, so a suppressed or outside-diff-range finding raised on an older, windowed-out
    round is missing from `suppressed=`/`cr_outside_diff=` below rather than merely counted as
    stale.

    Unlike `window_blind`, which settles a coverage or dating question once even one of a
    login's own reviews sits in view, a finding count needs every round read, not just a
    recent-enough one: an unread round can carry a finding of its own regardless of whether a
    newer round from the same login is visible. `hasPreviousPage` on this `last`-windowed
    connection means the reviews left unread are the oldest, exactly where an unanswered
    finding is most likely to still sit unresolved.
    """
    return bool(((pr.get("reviews") or {}).get("pageInfo") or {}).get("hasPreviousPage"))


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
    body = node.get("body") or ""
    opening = next((ln for ln in body.splitlines() if ln.strip()), "")
    return body if REFUSAL.search(opening) else ""


def quota_refusal(node: dict) -> bool:
    """True where a refusal on this node names the account quota rather than another cause.

    Read from `refusal_of`'s own return rather than the raw body, so its exemptions protect
    this reading too: a node that is not a refusal at all returns `""` here, on which `QUOTA`
    cannot match, rather than this needing its own opening-line anchor and fenced-quote guard.
    The distinction matters because the two remedies are opposite. A file-count refusal is
    cleared by splitting the pull request. A quota one is an account-level state that a
    re-request or a wait does not touch, so treating the two alike sends a reader at a remedy
    that does nothing here.
    """
    return bool(QUOTA.search(refusal_of(node)))


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
    head = pr["headRefOid"]
    refusals = [
        n
        for n in reviewer_nodes(pr, "reviews")
        if (n.get("commit") or {}).get("oid") == head and refusal_of(n)
    ]
    return max(refusals, key=lambda n: n.get("submittedAt") or "") if refusals else None


def head_reviews(pr: dict) -> list[dict]:
    """The reviewer's own reviews that cover the current head, refusals excluded.

    A refusal is not coverage. It is a formal review, `state: COMMENTED`, carrying the head's
    commit and raising no threads, so it satisfies every check a clean pass does and renders a
    digest identical to one. That is how a pull request of 301 changed files, one over the
    reviewer's limit, sat one command from merging on a review that never ran.

    One list rather than a predicate beside a filter, because coverage and the count of rounds on
    the head are read from the same set and a second spelling of it drifts from the first.
    """
    head = pr["headRefOid"]
    return [
        n
        for n in reviewer_nodes(pr, "reviews")
        if (n.get("commit") or {}).get("oid") == head and not refusal_of(n)
    ]


def reviewed_head(pr: dict) -> bool:
    """True where one of the reviewer's own reviews covers the current head's commit.

    The liveness query carries no bodies, so a refusal reads there as ordinary coverage. That is
    deliberate rather than a gap: it ends the wait, which is what a terminal outcome should do,
    and the full read every wait finishes with is what tells the two apart. No exit code and no
    merge decision is taken from the liveness reading.
    """
    return bool(head_reviews(pr))


def review_effort(pr: dict) -> tuple[str, str]:
    """The newest head review's effective effort and selection source.

    GitHub can render an inherited choice as `Default (Lite)`, `Default (Balanced)`, or
    `Default (Max)`. A bare level is explicit. The setting remains user-controlled, and this
    reader only reports metadata that the completed review body exposes.
    """
    reviews = head_reviews(pr)
    if not reviews:
        return "unknown", "unknown"
    newest = max(reviews, key=lambda n: n.get("submittedAt") or "")
    plain = FENCE.sub("", newest.get("body") or "")
    for line in plain.splitlines():
        match = EFFORT_LINE.fullmatch(line)
        if match:
            source = "default" if match.group(1) else "explicit"
            return (match.group(2) or match.group(3)).lower(), source
    return "unknown", "unknown"


def is_coverage_line(line: str) -> bool:
    """Whether this line is the reviewer stating its file coverage, rather than prose about it."""
    if FLEET_REVIEW.fullmatch(line):
        return True
    if COVERAGE_BULLET.match(line):
        return True
    return bool(COVERAGE_SENTENCE.match(line)) and "changed file" in line.lower()


def coverage_statements(body: str) -> list[str]:
    """The lines this round states its file coverage on, quotations excluded."""
    return [ln.strip() for ln in FENCE.sub("", body or "").splitlines() if is_coverage_line(ln)]


def read_coverage(line: str) -> tuple[int, int] | None:
    """The (reviewed, changed) counts the line states, or None where they cannot be believed.

    None covers a wording this has no vetted spelling for and a pair that cannot both be true
    alike, since each leaves the same question unanswered and each takes the same remedy. A round
    reporting it read more files than the pull request changed is not a round that read them all,
    it is a line this script is parsing wrongly, and reading it as full coverage fails open on
    exactly the statement that says something is off.
    """
    marker = FLEET_REVIEW.fullmatch(line)
    m = COVERAGE_COUNTS.search(line)
    if marker:
        reviewed, changed = marker.group(1), marker.group(2)
        return (int(reviewed), int(changed)) if int(reviewed) <= int(changed) else None
    if not m:
        return None
    reviewed, changed = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return (int(reviewed), int(changed)) if int(reviewed) <= int(changed) else None


def coverage_of(node: dict) -> tuple[str, str]:
    """This round's coverage reading, with the line it was read from.

    A round making no statement at all reads as unstated rather than unvetted. 28 of the 333
    bodies measured carry an overview and a change list and nothing more. The shape is recognized,
    but it cannot prove full diff coverage and therefore blocks the status gate.

    A wording that is coverage-shaped and parses to no counts is the failure whose remedy is
    fixing this script, since a gate that allows whatever it does not recognize stops gating as
    the wording drifts, which it has done once already for each of the two patterns above.
    """
    worst, detail = UNSTATED, ""
    for line in coverage_statements(node.get("body") or ""):
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
    worst, detail = UNSTATED, ""
    for node in head_reviews(pr):
        state, line = coverage_of(node)
        if SEVERITY.index(state) < SEVERITY.index(worst):
            worst, detail = state, line
    return worst, detail


def file_table(body: str) -> list[str]:
    """The paths the round's own file summary table names, in the order it names them.

    Quotations are dropped for the reason a coverage line's are: this change puts a table into
    the diff and a review of it quotes one, and a quoted table read as this round's own names
    files nobody reviewed.

    The header is what opens the table and any line that is not a row closes it, so a second
    table later in the body is read as a second table rather than as more of the first.
    """
    paths, reading = [], False
    for line in FENCE.sub("", body or "").splitlines():
        if TABLE_HEADER.match(line):
            reading = True
        elif (row := TABLE_ROW.match(line)) is None:
            reading = False
        elif reading and not TABLE_RULE.match(line):
            cell = row.group(1)
            paths.append(cell.strip().strip("`").strip())
    return [p for p in paths if p]


def changed_paths(pr: dict) -> tuple[list[str], bool]:
    """The paths this pull request changes, and whether the query's window cut the list short.

    Truncation is carried rather than hidden, since a path outside a short window reads exactly
    like a path the reviewer left out of its table, and the two take opposite readings.
    """
    files = pr.get("files") or {}
    paths = [n.get("path") or "" for n in (files.get("nodes") or [])]
    return [p for p in paths if p], bool((files.get("pageInfo") or {}).get("hasNextPage"))


def head_table(pr: dict) -> list[str]:
    """The file table the rounds covering the current head carry, from the first that has one.

    Any round on the head rather than the one whose counts decided the verdict, since rounds on
    one commit describe one diff, and a re-requested round states the counts again and carries no
    table at all. Thirteen commits here carry more than one round and one of those pairs is that
    shape exactly, so which of the two the verdict happens to read must not decide whether a
    table is found.

    Head-scoped for the reason the coverage line is, and this is the tighter half of the reading:
    three of this repository's four partial pull requests carry their table on the round before a
    push, describing a diff that is not the one being merged, and comparing that table against
    the current changed files would name files as unreviewed on the strength of a stale list.
    Those report as no table rather than as a comparison, which is the honest answer.
    """
    for node in head_reviews(pr):
        named = file_table(node.get("body") or "")
        if named:
            return named
    return []


def table_against_diff(pr: dict, counts: tuple[int, int] | None) -> str:
    """What the reviewer's own file table says about the files the counts leave unread.

    This reports and decides nothing. The exit code stays with the counts, because the table
    tracks them nowhere near well enough to overturn one, and the measurement is what says so.
    Over 348 review bodies on this repository and 121 on ptr727/Blog, the table names the whole
    changed set on partial and fully covered rounds alike: all seven partials on Blog name every
    changed file, as do two of the four here that carry a table. A reading identical under both
    outcomes discriminates neither, which is why a full table is reported as corroborating
    nothing rather than as the count being a miscount.

    The one arm that locates anything is a table shorter by exactly what the counts say went
    unread. #479 states 16 of 17 and names 16, omitting `GOVERNANCE.md`, and it is the only
    evidence on record that the unread file is a real file rather than an artifact of counting.
    It stays a lead rather than a verdict, since the table is prose the reviewer writes: #609
    states 61 of 62 and names 50, and #606 names `GOVENANCE.md`, a path no diff here carries.

    A path named that the diff does not carry is what disqualifies the naming arm, that typo
    being enough to drop a real file into the omissions and read it as the one nobody reviewed.
    """
    named = head_table(pr)
    if not named:
        return "no round covering this head carries a file table, so none of them locates the file"
    changed, truncated = changed_paths(pr)
    if truncated or not changed:
        return (
            f"the reviewer names {len(named)} files in its own table and the diff could not "
            f"be read back to compare them, the changed-file list being "
            f"{'longer than the window this reads' if truncated else 'absent from the query'}"
        )
    omitted = [p for p in changed if p not in named]
    invented = [p for p in named if p not in changed]
    short = 0 if counts is None else counts[1] - counts[0]
    if not omitted:
        return (
            f"the reviewer's own file table names all {len(changed)} changed files, which it "
            f"also does on rounds stating full coverage, so it corroborates nothing and "
            f"names no unread file"
        )
    if len(omitted) == short and not invented:
        return (
            f"the reviewer's own file table omits exactly the {short} file"
            f"{'' if short == 1 else 's'} the counts leave unread, naming "
            f"{', '.join(omitted)}. The table is prose the reviewer writes rather than a "
            f"list from the API, so that is a lead to check rather than a verdict"
        )
    return (
        f"the reviewer's own file table names {len(named)} of the {len(changed)} changed "
        f"files, omitting {len(omitted)} where the counts leave {short} unread"
        + (f" and naming {', '.join(invented)}, which the diff does not carry" if invented else "")
        + ", so it tracks the counts nowhere and names no unread file"
    )


def normal(text: str) -> str:
    """A marker reduced toward what a vetted list compares: ASCII, single spaces, counts as `(N)`.

    Letter case survives, and `unvetted` folds it at the membership test instead.

    The verdict headings carry a colored circle and the suppressed heading carries its finding
    count, so both drift on every review without the section having changed at all.
    """
    ascii_only = "".join(c for c in text if ord(c) < 128)
    return re.sub(r"\s+", " ", re.sub(r"\(\d+\)", "(N)", ascii_only)).strip()


def unvetted(marker: str, vetted: set[str]) -> bool:
    """Whether `marker` is absent from `vetted`, compared without regard to letter case."""
    # Folded here rather than in `normal`, whose value the report strings also carry.
    # A report naming a folded shape would not match the body it asks the reader to quote beside it.
    return marker.casefold() not in {v.casefold() for v in vetted}


def unrecognized_in(body: str) -> list[str]:
    """Every marker in one review body this script has no vetted spelling for.

    Read over the body with fenced blocks removed, for the reason the coverage line is: a review
    quoting a heading is not a review carrying one, and this script's own pull requests quote
    these lists in full.

    A body carrying no heading at all is reported rather than passed, since every one of the 333
    measured opens on a heading and a body with none is a format nothing here has seen. A refusal
    is the exemption, being a bare paragraph by design and already classified as one.
    """
    # A refusal is a bare paragraph rather than a review body, and `REFUSAL` is its spelling.
    # It carries no marker to check, and its own opening line is not a coverage statement.
    # Its wording drifting is still caught, since `refusal_of` then stops matching.
    # What is left of a drifted refusal is a body with no heading, which is the arm below.
    if refusal_of({"body": body}):
        return []
    plain = CODE_SPAN.sub("", FENCE.sub("", body or ""))
    headings = [normal(ln) for ln in plain.splitlines() if MARKDOWN_HEADING.match(ln)]
    labels = [normal(m.group(1)) for m in map(LABEL_LINE.match, plain.splitlines()) if m]
    found = [f"heading: {h}" for h in dict.fromkeys(headings) if unvetted(h, VETTED_HEADINGS)]
    found += [
        f"summary: {marker}"
        # Deduplicated on the raw text before normalizing, which is what it did before the hoist.
        for marker in (normal(s) for s in dict.fromkeys(SUMMARY.findall(plain)))
        if unvetted(marker, VETTED_SUMMARIES)
    ]
    found += [
        f"metadata label: {la}" for la in dict.fromkeys(labels) if unvetted(la, VETTED_LABELS)
    ]
    found += [
        f"coverage line: {ln}" for ln in coverage_statements(body) if read_coverage(ln) is None
    ]
    if not headings and not refusal_of({"body": body}):
        found.append("body carrying no heading at all, which no measured review body does")
    return found


def unrecognized_shapes(pr: dict) -> list[str]:
    """Everything about this pull request's reviewer output that this script cannot read.

    Every round rather than the head's, because this asks whether the reader still understands
    the reviewer rather than what the reviewer said about this commit. A shape that arrived one
    round ago is one every later round will carry.
    """
    found = []
    for node in reviewer_nodes(pr, "reviews"):
        where = ((node.get("commit") or {}).get("oid") or "")[:8] or "commit unknown"
        found += [f"{item}  (round {where})" for item in unrecognized_in(node.get("body") or "")]
    return found + reviewer_login_drift(pr)


def reviewer_login_drift(pr: dict) -> list[str]:
    """Logins that read as this reviewer without being the spelling every query here filters on.

    Read from the authors alone, so the liveness query answers it as well as the full one. That
    is what lets the wait stop on a drift rather than poll its whole timeout out against a review
    sitting in plain sight, which is the failure this reading exists to name.
    """
    logins = {
        (n.get("author") or {}).get("login") or ""
        for field in ("reviews", "comments")
        for n in ((pr.get(field) or {}).get("nodes") or [])
    }
    return [
        f"reviewer login: {login}, where every query here filters on {REVIEWER}"
        for login in sorted(logins)
        if login != REVIEWER and READS_AS_REVIEWER.search(login)
    ]


def report_verdict(pr: dict) -> int:
    """Print the blocking verdict's own status line, and return the exit code it carries.

    The unrecognized shape outranks the coverage one, because a reader that does not understand
    the output cannot be trusted about what it read of the diff either.
    """
    # A coverage line this cannot parse is one of the shapes below rather than a case of its own.
    # It exits here with the remedy that fits it, the reader being what needs the fix.
    if unrecognized_shapes(pr):
        print(
            f"status=UNRECOGNIZED_REVIEWER_OUTPUT this script does not know one or more shapes "
            f"in what the reviewer sent, listed above, so nothing it reports about this review "
            f"is trustworthy and the review loop does not close on this digest. File an issue "
            f"on {HUB} naming each shape and quoting the body it came from, since this script "
            f"is hosted there and the fix lands there. Merging this pull request anyway is the "
            f"maintainer's decision to take and not this script's, and not the agent's."
        )
        return 43
    # A missing head review has no coverage verdict yet.
    # The wait path owns that incomplete state.
    if not head_reviews(pr):
        return 0
    state, line = head_coverage(pr)
    if state == PARTIAL:
        # The unread count comes from the line that decided PARTIAL, never from past rounds.
        # None is unreachable there, and narrowing keeps a later change from crashing the gate.
        counts = read_coverage(line)
        if counts is None:
            gap = "that the counts on that line could not be re-read, so the total is unknown"
        else:
            unread = counts[1] - counts[0]
            gap = (
                f"{unread} of the {counts[1]} changed files "
                f"{'has' if unread == 1 else 'have'} no review"
            )
        print(
            f"status=COVERAGE_IS_PARTIAL the review covering the head read fewer files than the "
            f"pull request changed, so part of the diff has no review at all. Every partial on "
            f"record stayed partial at the identical ratio across every later round, so a "
            f"re-request is not the remedy it reads as. Splitting is "
            f"the remedy where it applies, and it does not apply to a promotion. Otherwise this "
            f"is the maintainer's call, taken knowing {gap}, and knowing that "
            f"{table_against_diff(pr, counts)}"
        )
        return 42
    if state == UNSTATED:
        print(
            "status=COVERAGE_IS_UNSTATED the review covering the head states no changed-file "
            "coverage, so the review loop cannot prove that it read the full diff. Confirm the "
            "head branch carries the current code-review skill and Copilot instructions, then "
            "request another review. Merging without coverage is the maintainer's decision, "
            "not the agent's."
        )
        return 45
    return 0


def age(stamp: str, now: datetime) -> float | None:
    """Seconds from `stamp` to `now`, or None where the stamp is absent or unparseable.

    None is returned rather than zero or a raise, because every caller treats an unknown age as
    "cannot judge this" and reports nothing. Zero would read as a check that just started, which
    is the reading that reports a job stuck for hours as fresh.
    """
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    # ValueError is the unparseable stamp, and TypeError is the parseable one carrying no zone.
    # A stamp with no offset parses to a naive datetime, which will not subtract from an aware now.
    # Catching only the first lets that raise out of a reporting call and take the digest with it.
    # A crash is the worst outcome here, since the whole point is to say what the state is.
    except (ValueError, TypeError):
        return None


def head_commit(pr: dict) -> dict:
    """The commit object whose oid is the head's, or an empty dict where none is.

    One reader, because three functions traversed this same shape in three slightly different
    ways and a payload change would have had to be caught in all three. Selection is by oid
    rather than by position, since a rollup off any other commit describes a push ago and renders
    every field, which is a review counted without being read one field along.
    """
    head = pr.get("headRefOid") or ""
    return next(
        (
            c.get("commit") or {}
            for c in ((pr.get("commits") or {}).get("nodes") or [])
            if (c.get("commit") or {}).get("oid") == head
        ),
        {},
    )


def check_nodes(pr: dict) -> list[dict]:
    """The head commit's checks, each as {name, state, conclusion, since}.

    A rollup carries two node shapes and they spell every field differently: a CheckRun has a
    `name`, a `status` and a `conclusion`, while a StatusContext has a `context` and a single
    `state` that folds both. Reading one shape's keys off the other yields None for all of them,
    which scores an external status as a check in no state at all, so neither pending nor failed.
    They are normalized here so one reading serves both.
    """
    # No match reports nothing rather than falling back to another commit's rollup.
    # A fallback is that same stale reading reached by a different route.
    # The absence is not silent either, and `checks_unreadable` is where the digest says it.
    rollup = head_commit(pr).get("statusCheckRollup") or {}
    out = []
    for n in (rollup.get("contexts") or {}).get("nodes") or []:
        if n.get("__typename") == "CheckRun":
            out.append(
                {
                    "name": n.get("name") or "",
                    "state": n.get("status") or "",
                    "conclusion": n.get("conclusion") or "",
                    "since": n.get("startedAt") or "",
                }
            )
        elif n.get("__typename") == "StatusContext":
            # A StatusContext reports one field for both, so its state doubles as its conclusion.
            # Its PENDING means the posting system reported the run as under way.
            # A CheckRun's PENDING means the opposite, dispatched and not begun.
            # So the same string is two states, and the shape is knowable only here.
            # Left alone, a long external build reports as queued with no runner assigned.
            # That names a cause the run does not have, on a system that did pick it up.
            state = n.get("state") or ""
            out.append(
                {
                    "name": n.get("context") or "",
                    "state": "IN_PROGRESS" if state == "PENDING" else state,
                    "conclusion": state,
                    "since": n.get("createdAt") or "",
                }
            )
        else:
            # A third union member is skipped rather than forced into the StatusContext shape.
            # Forcing it reads a label off a node spelling it otherwise, so it renders nameless.
            # It also reads a state that is not there, so it reports as a red check.
            # So skipping is right and skipping *quietly* is not, which review caught here.
            # An unread check missing from the tally is this script's own core failure.
            # The name carries the typename so the digest can say which member it could not read.
            out.append(
                {
                    "name": "",
                    "state": "",
                    "conclusion": "",
                    "since": "",
                    "unreadable": n.get("__typename") or "an unnamed type",
                }
            )
    return out


def check_shape(node: dict, now: datetime, grace: float, stall: float) -> str:
    """How this check is stuck, or the empty string where it is not.

    Four shapes, because `mergeStateStatus` collapses all of them into `BLOCKED` and each wants
    a different response. A run this session spent twenty-five minutes polling `BLOCKED` on a
    pull request whose only unfinished check was a rollup job no runner ever took, and the cause
    came from the maintainer rather than from any reading here.

    NOT_POSTED is a required status whose poster has not spoken, which is a StatusContext's
    `EXPECTED` and only ever that. It is not a starved job and must not borrow that remedy: no
    runner is owed a status nothing has posted, so re-running a workflow clears nothing, and
    reported as NOT_PICKED_UP it sends a reader at the runner pool over a missing poster.

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
    state, conclusion = node.get("state") or "", node.get("conclusion") or ""
    elapsed = age(node.get("since") or "", now)
    if state in NOT_POSTED:
        # Its own shape, because the starved remedy is wrong for it in both directions.
        # No runner is owed a status nothing has posted, so re-running a workflow clears nothing.
        # Reported under NOT_PICKED_UP it sent a reader at the runner pool over a missing poster.
        return "NOT_POSTED" if elapsed is not None and elapsed > grace else ""
    if state in NOT_STARTED:
        return "NOT_PICKED_UP" if elapsed is not None and elapsed > grace else ""
    if state == "IN_PROGRESS":
        return "RUNNING_LONG" if elapsed is not None and elapsed > stall else ""
    # A finished check carrying no conclusion yet is still settling, so nothing is reported.
    # Reading an absent verdict as a failure invents a red check out of a race in the API.
    # An unrecognized state would reach the line below and produce that same false failure.
    if not conclusion:
        return ""
    # A conclusion this does not recognize is reported rather than passed over.
    # An unknown verdict blocking a merge is exactly what a reader needs told.
    # A new enum member read as a pass is a red check rendering as a green digest.
    return "" if conclusion in CHECK_OK else "FAILED"


def checks_stuck(
    nodes: list[dict], now: datetime, grace: float, stall: float
) -> list[tuple[dict, str]]:
    """Every check in a stuck shape, as (node, shape), in the order the rollup returns them.

    Takes the normalized list rather than the payload, so one `check_nodes` call serves the tally
    and this together. Two calls parsed the same rollup twice per digest, and this script's whole
    reason for existing is that the reading is the cost.
    """
    return [
        (n, s)
        for n in nodes
        if not n.get("unreadable") and (s := check_shape(n, now, grace, stall))
    ]


def checks_truncated(pr: dict) -> bool:
    """True where the head's rollup carries more contexts than the query asked for.

    The same guard `window_blind` is for, one connection along. A rollup past a hundred contexts
    would drop the rest silently, and a required check among them would be missing from both the
    tally and the stuck reading, so the digest would render a clean pass over a check it never saw.
    That is the exact false clean the rest of this script exists to prevent, and a fleet repository
    with a large matrix build reaches a hundred contexts far sooner than this one does.
    """
    contexts = (head_commit(pr).get("statusCheckRollup") or {}).get("contexts") or {}
    return bool((contexts.get("pageInfo") or {}).get("hasNextPage"))


def checks_unread(nodes: list[dict]) -> list[str]:
    """The rollup typenames this could not read, in the order they appeared.

    A union member that is neither a CheckRun nor a StatusContext cannot be normalized, and it is
    reported rather than dropped. Dropping it is the silent narrowing every other guard here
    exists against: a check absent from the tally and from the stuck reading renders as a clean
    pass over something never seen. Raised in review on this change, against the commit that
    chose skipping over forcing, which was the right half of the answer on its own.
    """
    return [n["unreadable"] for n in nodes if n.get("unreadable")]


def checks_unreadable(pr: dict) -> bool:
    """True where the payload carries commits but none of them is the head.

    `check_nodes` reports nothing in that case rather than reading another commit's rollup, and
    nothing is indistinguishable from a pull request with no checks. So the state is named instead
    of left to render as `checks=0/0`, which a reader would take as a fact about the head rather
    than as this reading having failed. A silent narrowing is the failure mode this whole script
    is built against, and it does not get an exception for its own newest field.
    """
    return bool((pr.get("commits") or {}).get("nodes") or []) and not head_commit(pr)


def checks_tally(nodes: list[dict]) -> tuple[int, int]:
    """(checks that have passed, checks there are), so a bare count says how far the head is."""
    read = [n for n in nodes if not n.get("unreadable")]
    return sum(1 for n in read if (n.get("conclusion") or "") in CHECK_OK), len(read)


def live_state(owner: str, repo: str, num: int) -> tuple[str, bool, dict | None]:
    """Return (head_sha, copilot_reviewed_current_head, copilot_answer_outside_a_review)."""
    pr = gql(Q_LIVE, owner, repo, num)
    return pr["headRefOid"], reviewed_head(pr), answered_outside_review(pr)


def heading_of(block: str) -> str:
    """The block's own heading, unwrapped from `<summary>` where it wears one.

    A block starts at its heading, so the heading is the first line whatever markup it carries.
    Reading further would let a finding's own text supply a count the heading never carried, and
    a wrong count reads exactly like a right one.
    """
    head = block.lstrip()
    m = SUMMARY.search(head)
    return m.group(1) if m and head.lower().startswith("<summary") else head.split("\n", 1)[0]


# A blockquote marker (`>` per line, Markdown's own quoting convention) sits in front of every line CodeRabbit wraps its outside-diff section in.
# `HEADING`'s `\s*` does not match `>`, so a heading under one reads as prose without this stripped first.
# Copilot's own shapes carry no such prefix, so this is only ever asked of CodeRabbit's own marker below, never of `SUPPRESSED`.
BLOCKQUOTE = re.compile(r"^>+\s?")
# `<details>(.*?)</details>` lazily pairs each open with the *next* close, which is the innermost one once a shape nests, silently losing everything the outer wrapper still carries after it.
# CodeRabbit's outside-diff section does exactly that: a file wrapper nested inside the section heading, itself wrapping a per-finding "Prompt for AI Agents" block three levels deep.
DETAILS_TAG = re.compile(r"<details(?:\s[^>]*)?>|</details>", re.IGNORECASE)


def details_regions(body: str) -> tuple[list[str], str]:
    """Every top-level `<details>...</details>` region's own content, plus what is left once
    every top-level region is removed whole.
    """
    regions: list[str] = []
    leftover: list[str] = []
    depth = 0
    region_start = 0
    cursor = 0
    for m in DETAILS_TAG.finditer(body):
        opening = not m.group().startswith("</")
        if opening:
            if depth == 0:
                leftover.append(body[cursor : m.start()])
                region_start = m.end()
            depth += 1
        elif depth > 0:
            depth -= 1
            if depth == 0:
                regions.append(body[region_start : m.start()])
                cursor = m.end()
    leftover.append(body[cursor:])
    return regions, "".join(leftover)


def marker_blocks(body: str, marker: re.Pattern[str], strip_blockquote: bool = False) -> list[str]:
    """Return the review body's sections whose own heading matches `marker`, each sliced from
    that heading through to the end of its own region.

    Shared by `suppressed_blocks` and `outside_diff_blocks`. `strip_blockquote` affects heading
    detection only, never the returned content.
    """
    if not body:
        return []
    # Each wrapper's contents, plus what is left outside them all, so a heading is found anywhere.
    # The regions do not overlap, since `details_regions` removes exactly what it returns.
    regions, leftover = details_regions(body)
    regions = [*regions, leftover]
    blocks = []
    for region in regions:
        raw_lines = region.splitlines()
        # Scanned line by line rather than read from a wrapper's own summary.
        # A heading can sit in the wrapper's body instead, where reading only the summary would miss it.
        scan_lines = [BLOCKQUOTE.sub("", ln) for ln in raw_lines] if strip_blockquote else raw_lines
        for i, line in enumerate(scan_lines):
            if marker.search(line) and (HEADING.match(line) or COUNT.search(line)):
                blocks.append("\n".join(raw_lines[i:]))
                break
    return blocks


def suppressed_blocks(body: str) -> list[str]:
    """Copilot's own low-confidence findings, collapsed rather than raised as inline threads.

    See `marker_blocks` for the shared reading.
    """
    return marker_blocks(body, SUPPRESSED)


def outside_diff_blocks(body: str) -> list[str]:
    """CodeRabbit's own outside-diff-range findings: a real finding on a line outside the pull
    request's changed hunks, which GitHub cannot attach as an inline review comment, so
    CodeRabbit collapses it into the review body instead. Observed corpus, ptr727/ProjectTemplate
    PR #1053: a `<summary>...Outside diff range comments (N)</summary>` heading, nested one
    file-level `<details>` deep in turn nesting a per-finding "Prompt for AI Agents" block,
    wrapped in the review's own blockquote.

    Reads a section spanning several files or several findings in one file just as reliably as a
    single finding, since `details_regions` counts the nesting rather than pairing the first
    open it meets with the first close.
    """
    return marker_blocks(body, CR_OUTSIDE_DIFF, strip_blockquote=True)


def finding_count(block: str) -> int:
    """The heading's `(N)`, floored at one, since a block reported as zero reads as a clean pass."""
    m = COUNT.search(heading_of(block))
    return max(int(m.group(1)), 1) if m else 1


def qodo_review_comment(pr: dict) -> dict | None:
    """The reviewer's newest `Code Review by Qodo` comment, where its actual findings live.

    Its formal review object carries an empty body on every review checked, confirmed across
    roughly 60. The findings ride a PR-level comment instead, alongside a second one, `PR
    Summary by Qodo`, that carries none, told apart by this comment's own heading. Comments carry
    no commit, so this is read by recency rather than by head, the same limitation
    `answered_outside_review` already has for Copilot's own plain comments.
    """
    comments = [
        c
        for c in reviewer_nodes(pr, "comments", QODO_LOGIN)
        if QODO_REVIEW_HEADING.search(c.get("body") or "")
    ]
    return max(comments, key=lambda c: c.get("createdAt") or "", default=None)


def qodo_open_findings(body: str) -> list[str]:
    """Each numbered finding in a `Code Review by Qodo` comment that carries neither Qodo's own
    `Resolved` nor `Dismissed` self-tracked badge, so it is still open.

    Qodo nests each finding's own Description/Code/Relevance/Evidence/Agent-prompt sections
    under `<summary>` tags of their own, so every `<summary>` in the comment is read rather than
    only the outermost ones, and only the numbered heading itself, matched by `QODO_FINDING`,
    counts as a finding.
    """
    if not body:
        return []
    return [
        s.strip()
        for s in SUMMARY.findall(body)
        if QODO_FINDING.match(s) and not QODO_BADGE.search(s)
    ]


def qodo_comments_blind(pr: dict) -> bool:
    """True where Qodo's own `Code Review by Qodo` comment specifically can be sitting behind
    the comments window, so `qodo_open` cannot tell "never commented" from "commented, unseen".

    `window_blind(pr, "comments", QODO_LOGIN)` alone is not enough: it clears the moment any
    Qodo comment is visible, including the paired `PR Summary by Qodo` that carries no findings
    of its own, so a findings comment old enough to fall out of the window would read as though
    Qodo had never posted one at all. A second check closes that gap: an older page exists, some
    Qodo comment is visible, yet `qodo_review_comment` still finds none matching the findings
    heading among what is visible, so the one that matters is the one sitting behind the cut.
    """
    if window_blind(pr, "comments", QODO_LOGIN):
        return True
    older = ((pr.get("comments") or {}).get("pageInfo") or {}).get("hasPreviousPage")
    return (
        bool(older)
        and bool(reviewer_nodes(pr, "comments", QODO_LOGIN))
        and not qodo_review_comment(pr)
    )


def thread_author(t: dict) -> str:
    """The login on a thread's opening comment, or `""` where there is none to read.

    A deleted account leaves `author` present and null, which a chained `.get` would crash on
    without the default here.
    """
    return (((t.get("comments") or {}).get("nodes") or [{}])[0].get("author") or {}).get(
        "login"
    ) or ""


def digest(
    owner: str,
    repo: str,
    num: int,
    seen: set[str] | None = None,
    pr: dict | None = None,
    stalled: str | None = None,
    now: datetime | None = None,
    grace: float = CHECK_GRACE,
    stall: float = CHECK_STALL,
    checks: list[dict] | None = None,
) -> tuple[str, int]:
    """Render the digest from a caller's payload and normalized checks when supplied.

    The caller passes its own readings when the exit code has to agree with what was printed,
    since a review landing between two reads makes a fresh fetch describe a different pull
    request than the one the code was decided from. `checks` spends the rollup parse once,
    since the wait decides an exit code from the reading it just printed.
    `now` is a parameter for the same reason, so a case can hold a check at a known age rather
    than at whatever the clock says when the suite runs.

    `stalled` remains as an ignored compatibility parameter for callers that supplied the old
    pickup reading. GitHub's effort-labeled lifecycle makes that reading inconclusive.
    """
    pr = gql(Q_FULL, owner, repo, num) if pr is None else pr
    now = datetime.now(UTC) if now is None else now
    head = pr["headRefOid"]
    revs = reviewer_nodes(pr, "reviews")
    # `revs` is every round and `on_head` is the ones that reviewed this commit.
    # A refusal sits in the first and not the second, being a round that covered nothing.
    on_head = head_reviews(pr)
    effort, effort_source = review_effort(pr)
    cover, cover_line = head_coverage(pr)
    unknown = unrecognized_shapes(pr)
    threads = pr["reviewThreads"]["nodes"]
    # True where the connection cut off before this pull request's actual thread count (#973).
    # Read here rather than inline below, since both the summary line and the explanatory block need it.
    truncated = threads_truncated(pr)
    # Same reasoning, the `reviews` connection rather than `reviewThreads`, feeding `suppressed=` and `cr_outside_diff=` below.
    revs_truncated = reviews_truncated(pr)
    # Any known reviewer's own thread, not only Copilot's.
    # An open thread blocks a ruleset-gated merge whoever opened it, and counting Copilot's alone hid a CodeRabbit/qodo thread that did block one (PR #915).
    # `thread_author` carries the deleted-account default this needs.
    unresolved = [t for t in threads if not t["isResolved"] and thread_author(t) in KNOWN_REVIEWERS]
    # A breakdown beside the raw count, but only where more than one reviewer contributes to it.
    # A single reviewer's own count is what `unresolved=N` already meant before this generalized.
    # Printing one name beside its own total says nothing the number did not already say.
    by_login = {
        login: sum(1 for t in unresolved if thread_author(t) == login) for login in KNOWN_REVIEWERS
    }
    contributors = {login: n for login, n in by_login.items() if n}
    breakdown = (
        " (" + " ".join(f"{login}={n}" for login, n in contributors.items()) + ")"
        if len(contributors) > 1
        else ""
    )

    # Every round, not just the head, because a suppressed finding has no resolved state to read.
    # Head-scoping treated "superseded by a push" as "answered", and the two are not the same.
    # A finding nobody replied to left the digest the moment the branch moved, reporting zero.
    # That is how four rounds went unanswered across three pull requests in one day.
    # The head is still marked per block, since a finding on an older round may be moot.
    # Deciding that is the reader's call rather than one the count makes for them.
    blocks = [(n, b) for n in revs for b in suppressed_blocks(n.get("body") or "")]
    on_head_blocks = [b for n, b in blocks if (n.get("commit") or {}).get("oid") == head]
    stale = sum(finding_count(b) for n, b in blocks) - sum(finding_count(b) for b in on_head_blocks)

    answer = answered_outside_review(pr)
    # Spent where coverage of the same head landed, the precedence the exit codes already hold.
    # Reported regardless, it prints `review_on_head=yes refusal=YES` over a reviewed head.
    # That tells a reader to split a pull request the reviewer has just reviewed.
    refusal = None if on_head else refusing_review(pr)
    # Read once and handed to the line below, since `quota_refusal` re-walks `refusal_of`.
    refusal_field = "no" if not refusal else ("QUOTA" if quota_refusal(refusal) else "YES")
    blind = [f for f in ("reviews", "comments") if window_blind(pr, f)]
    answered = "yes" if answer else ("unknown" if blind else "no")
    # Normalized once and handed to both readers, since the parse is the cost here.
    # The caller's own list wins where it has one, so the wait parses the rollup once.
    # Without that it read once for what it prints and again for what it returns.
    checks = check_nodes(pr) if checks is None else checks
    ok, total = checks_tally(checks)
    stuck = checks_stuck(checks, now, grace, stall)
    # Which other known reviewers have posted anything at all on this exact head, identity and commit only, no body read.
    # Whether it is a clean pass, a finding, or a refusal of its own is each bot's own prose to parse.
    # Two shapes of that are read below rather than left as each bot's own future task: CodeRabbit's outside-diff-range blocks and Qodo's comment-only findings.
    # Coverage and refusal reading stay Copilot-only, each of the others writing its own findings in its own format.
    # Omitted entirely where none has, so a repository not trialing either stays silent.
    other_on_head = [
        login
        for login in OTHER_REVIEWERS
        if any(
            (n.get("commit") or {}).get("oid") == head for n in reviewer_nodes(pr, "reviews", login)
        )
    ]
    # The service name each tracked other-reviewer's own rate-limit marker gives, where its newest comment or review on this pull request carries one.
    # Not head-scoped, unlike `other_on_head`, since the one observed marker rides a plain comment, which carries no commit to compare against the head at all.
    other_limited = {
        login: name for login in OTHER_REVIEWERS if (name := rate_limited_by(pr, login))
    }
    # CodeRabbit's own outside-diff-range findings, the identical blind spot `blocks` above reads for Copilot.
    # A real finding collapsed into the review body rather than raised as an inline review comment opens no `reviewThreads` entry either.
    # Read every round, not only the head, for the same reason `blocks` is: a finding nobody replied to must not drop out of the digest the moment a later push supersedes its own round.
    cr_revs = reviewer_nodes(pr, "reviews", CODERABBIT_LOGIN)
    cr_blocks = [(n, b) for n in cr_revs for b in outside_diff_blocks(n.get("body") or "")]
    cr_on_head_blocks = [b for n, b in cr_blocks if (n.get("commit") or {}).get("oid") == head]
    cr_stale = sum(finding_count(b) for n, b in cr_blocks) - sum(
        finding_count(b) for b in cr_on_head_blocks
    )
    # Qodo's own comment-only findings: its formal review carries no body at all on any round checked, so its numbered findings are read from its `Code Review by Qodo` PR comment instead.
    # Comments carry no commit, so this is read by recency, not head-scoped the way `cr_blocks` above is.
    qodo_comment = qodo_review_comment(pr)
    qodo_open = qodo_open_findings(qodo_comment.get("body") or "") if qodo_comment else []
    lines = [
        # The repository leads the line, since a number alone reads as correct anywhere.
        # A digest of the wrong pull request is well-formed, so naming it is what shows the miss.
        f"repo={owner}/{repo} pr={num} head={head[:8]} rounds={len(revs)} "
        f"review_on_head={'yes' if on_head else 'NO'} "
        # Present only where at least one other tracked reviewer has posted on this exact head.
        # No verdict rides on it, unlike `review_on_head`, since nothing here reads what a CodeRabbit or qodo round said, only that one landed.
        + (f"other_reviewed={','.join(other_on_head)} " if other_on_head else "")
        # Present only where a tracked other-reviewer's newest word is that marker.
        # The common case, neither is rate-limited right now, stays silent rather than printing `none`.
        + (
            f"other_rate_limited={','.join(f'{login}:{name}' for login, name in other_limited.items())} "
            if other_limited
            else ""
        )
        + f"effort={effort} effort_source={effort_source} "
        # A field of its own beside that one, since a round can cover the head and read part.
        # Those two readings are what `review_on_head=yes` alone conflates.
        f"coverage={COVERAGE_FIELD[cover]} "
        # Every other field on this line is a reading of the review.
        # This one says whether the readings can be believed at all, so it is not a count.
        f"shapes={'UNRECOGNIZED' if unknown else 'ok'} "
        # A field of its own, since `rounds=1 review_on_head=NO` is also what a stale round is.
        # The two want opposite responses, one a re-request and the other a split pull request.
        # Upper-case for the reason `NO` is, as a state that blocks a merge is not one to skim.
        # `QUOTA` is its own value rather than folded into `YES`, since its remedy is neither a re-request nor a split.
        # It is the account-level state `status`/`wait` name distinctly.
        f"refusal={refusal_field} "
        # A trailing `+` says the count right before it undercounts, rather than a reader having to notice a fourth field further down to learn the same thing.
        # `unresolved` is drawn from the same truncated `threads` list, so a cut page can hide an open thread exactly as easily as it hides a resolved one, and both counts carry the marker.
        f"threads={len(threads)}{'+' if truncated else ''} "
        f"unresolved={len(unresolved)}{'+' if truncated else ''}{breakdown} "
        # A trailing `+` on either count says the same as it does on `threads=`/`unresolved=` above: a round old enough to fall out of the `reviews` window is a round its own finding cannot be read from.
        f"suppressed={sum(finding_count(b) for n, b in blocks)}{'+' if revs_truncated else ''} "
        f"(on_head={sum(finding_count(b) for b in on_head_blocks)} earlier={stale}) "
        # Present where CodeRabbit has raised at least one outside-diff-range finding on any round, or the truncated window means one could exist unseen.
        # A repository not trialing it, on an untruncated window, stays silent rather than printing a permanent `cr_outside_diff=0`.
        + (
            f"cr_outside_diff={sum(finding_count(b) for n, b in cr_blocks)}{'+' if revs_truncated else ''} "
            f"(on_head={sum(finding_count(b) for b in cr_on_head_blocks)} earlier={cr_stale}) "
            if cr_blocks or revs_truncated
            else ""
        )
        # Present once Qodo has posted a `Code Review by Qodo` comment at all, `0` included.
        # A `0` here is itself a reading, Qodo reviewed and left nothing open, rather than silence about whether it reviewed.
        # `unknown` where its findings comment specifically can be sitting behind the window, so its silence here cannot be told apart from it never having commented.
        # Absent only where `qodo_comments_blind` clears it and it genuinely never has.
        + (
            f"qodo_open={len(qodo_open)} "
            if qodo_comment
            else ("qodo_open=unknown " if qodo_comments_blind(pr) else "")
        )
        + f"answered_outside_review={answered} "
        f"requested={'yes' if reviewer_requested(pr) else 'no'} "
        f"merge={pr.get('mergeStateStatus')} "
        # `merge=BLOCKED` names no cause and is worn by every gate alike.
        # A red check, a check nothing runs, an open thread, and a missing approval all read it.
        # One run here polled that word for twenty-five minutes without learning which it was.
        # The tally says how far the head got, and `checks=0/0` says the rollup carried nothing.
        f"checks={ok}/{total}"
        # Named only where there is one to name.
        # A field reading `none` on every green run is one a reader skips when it finally says more.
         + (f" stuck={','.join(sorted({s for _, s in stuck}))}" if stuck else "")
    ]
    if refusal:
        # Printed whole for the reason the comment below is, as the wording carries the remedy.
        # A file-count refusal is cleared by splitting the pull request and a quota one by waiting.
        # This reads neither cause, only that the round declined.
        lines.append(
            "  COPILOT REFUSED THIS ROUND: the review carrying the head says it did "
            "not review, so it covers nothing and re-requesting the same head repeats "
            "it, and the body below is what says which remedy applies"
        )
        lines += [
            f"    {ln.rstrip()}" for ln in (refusal.get("body") or "").splitlines() if ln.strip()
        ]
    if unknown:
        # First of the blocks, since it says how far the rest of them can be trusted.
        lines.append(
            f"  UNRECOGNIZED REVIEWER OUTPUT ({len(unknown)}): the shapes below are ones "
            "this script has no reader for, so every other field here is a reading of "
            "output it does not fully understand and a clean digest does not mean a "
            f"clean review. File an issue on {HUB}, which hosts this script, naming each "
            "shape and quoting the body it came from, before closing the review loop. "
            "Whether to merge anyway is the maintainer's call rather than the agent's"
        )
        lines += [f"    {item}" for item in unknown]
    if cover == PARTIAL:
        # The line prints under the marker for the reason a suppressed block does.
        # The counts say how much of the diff went unread, and no thread carries them.
        lines.append(
            "  COVERAGE IS PARTIAL: the review covering the head read fewer files than "
            "the pull request changed, so files in the diff have no review at all. A "
            "re-request has never cleared this on record, so report it rather than "
            "retrying into it, and let the maintainer take the merge decision"
        )
        lines.append(f"    {cover_line}")
        # The counts decide the state and this line says what the reviewer's file table adds.
        # Printed under the same marker, since a maintainer taking the decision wants both.
        # It names no verdict of its own, having been measured against the counts it sits under.
        # The table tracks them nowhere, which is why it is reported rather than read.
        lines.append(f"    {table_against_diff(pr, read_coverage(cover_line))}")
    for node, shape in stuck:
        # Each shape carries its own remedy, which is the whole point of telling them apart.
        # A reader handed one word for all three retries the wrong thing, or waits on a queue.
        elapsed = age(node.get("since") or "", now)
        # Clamped for display only, since a machine clock behind GitHub's renders `queued -3m`.
        # A negative age reads as nonsense and hides how long the thing has actually waited.
        # The comparison above keeps the raw value, so skew cannot make a stuck check report.
        mins = "age unknown" if elapsed is None else f"{int(max(elapsed, 0) // 60)}m"
        # Read with `.get` for the reason `age` catches two exceptions.
        # A caller handing this an odd node shape should cost a field, never the whole digest.
        # Reporting the state is the one job here, so it must not be the thing that raises.
        name = node.get("name") or "unnamed"
        if shape == "NOT_PICKED_UP":
            lines.append(
                f"  CHECK NOT PICKED UP ({name!r}, queued {mins} with no "
                "runner assigned): nothing here starts it, because the runner pool is "
                "GitHub-hosted, so re-run the workflow or wait on that capacity"
            )
        elif shape == "NOT_POSTED":
            lines.append(
                f"  CHECK NEVER POSTED ({name!r}, expected {mins} and not "
                "reported): a required status whose poster has not spoken, so no runner "
                "is owed it and re-running a workflow here clears nothing"
            )
        elif shape == "RUNNING_LONG":
            lines.append(
                f"  CHECK RUNNING LONG ({name!r}, running {mins}): it has a "
                "runner, so it is not starved, and whether this is hung or merely slow "
                "is a judgment against what this job normally costs"
            )
        else:
            lines.append(
                f"  CHECK FAILED ({name!r}, {node.get('state')}/"
                f"{node.get('conclusion')}): a verdict rather than a stuck check, so "
                "read the run and fix what failed, since no wait and no re-run clears "
                "a real failure"
            )
    unread = checks_unread(checks)
    if unread:
        lines.append(
            f"  CHECKS PARTIALLY UNREAD ({', '.join(sorted(set(unread)))}): the rollup "
            "carries a context type this cannot normalize, so it is absent from the "
            "tally and the stuck reading alike and neither speaks for it"
        )
    if checks_truncated(pr):
        lines.append(
            f"  CHECKS TRUNCATED: the head carries more than the {CHECKS_WINDOW} contexts "
            "this query asks for, so a check past the window is missing from the tally "
            "and from the stuck reading alike, and neither reports what it did not see"
        )
    if checks_unreadable(pr):
        lines.append(
            "  CHECKS UNREADABLE: the payload carries commits and none of them is the "
            "head, so no rollup here describes this head and `checks=0/0` is this "
            "reading failing rather than a pull request with no checks"
        )
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
        # Printed whole for the same reason a suppressed finding is, since it reaches no thread.
        # Its wording is the only thing separating a refusal from an ordinary remark.
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
        c = (t.get("comments") or {}).get("nodes", [{}])[0]
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
    for n, b in blocks:
        # Printed whole where a thread body is truncated, since a thread can be re-read at its id.
        # A suppressed finding has none, so this digest is the only place it appears.
        # GraphQL returns a null commit for a pending or partial review.
        # An empty sha rendered as "raised on , earlier round", losing what traces the finding.
        sha = ((n.get("commit") or {}).get("oid") or "")[:8]
        if not sha:
            where = "commit unknown, treat as outstanding"
        elif sha == head[:8]:
            where = "on head"
        else:
            where = f"raised on {sha}, earlier round"
        lines.append(
            f"  SUPPRESSED ({where}): no thread to resolve, "
            "answer it in the PR conversation quoting the finding"
        )
        # Indentation is kept, since a block carries fenced code a flattened line would garble.
        lines += [f"    {ln.rstrip()}" for ln in TAGS.sub("", b).splitlines() if ln.strip()]
    for n, b in cr_blocks:
        # Mirrors the `SUPPRESSED` rendering above, its own reasons applying identically here.
        sha = ((n.get("commit") or {}).get("oid") or "")[:8]
        if not sha:
            where = "commit unknown, treat as outstanding"
        elif sha == head[:8]:
            where = "on head"
        else:
            where = f"raised on {sha}, earlier round"
        lines.append(
            f"  CODERABBIT OUTSIDE-DIFF ({where}): no thread to resolve, "
            "answer it in the PR conversation quoting the finding"
        )
        lines += [f"    {ln.rstrip()}" for ln in TAGS.sub("", b).splitlines() if ln.strip()]
    for finding in qodo_open:
        # Not head-scoped, since the comment it comes from carries no commit at all.
        lines.append(
            "  QODO OPEN FINDING: neither Resolved nor Dismissed, no thread to resolve, "
            "answer it in the PR conversation quoting the finding, and spot-verify Qodo's "
            "own badge against `gh pr diff` rather than trusting it outright"
        )
        lines.append(f"    {' '.join(finding.split())}")
    if seen is not None:
        lines[0] += f" new={new}"
    return "\n".join(lines), len(unresolved)


def origin_owner() -> str | None:
    """The owner of the checkout this script sits in, or None where that cannot be read.

    Anchored on the script's own directory rather than the working directory, because this is
    reached from a hub checkout while the repository being answered is named on the command line,
    so the working directory says nothing about who owns either.
    """
    try:
        url = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        ).stdout.strip()
    # A missing git, a timeout, or any other failure all mean the owner cannot be read.
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$", url)
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
        return False, (
            "this checkout has no readable `origin`, so the owner a write would stay "
            "within cannot be established, and an unverified scope is not a scope"
        )
    if target_owner.lower() != origin:
        return False, (
            f"the target is under {target_owner}, and this checkout is under {origin}. "
            "A different owner is the shape this refuses outright: take it through the "
            "runbook mutations, where the write-guard hook reads the maintainer grant"
        )
    return True, ""


def first_comment(thread: dict) -> dict:
    """The thread's opening comment, which is the finding itself."""
    return ((thread.get("comments") or {}).get("nodes") or [{}])[0]


def unresolved_threads(owner: str, repo: str, num: int) -> list[dict]:
    """Every unresolved review thread, following the cursor to the end.

    Stopping at the first page reports no match on a thread that is merely further along, and a
    no-match is indistinguishable from a thread that was already answered.
    """
    out: list[dict] = []
    after = None
    while True:
        extra: dict[str, str] = {"after": after} if after else {}
        conn = gh_graphql(Q_THREADS, o=owner, r=repo, n=num, **extra)["repository"]["pullRequest"][
            "reviewThreads"
        ]
        out += [t for t in conn["nodes"] if not t["isResolved"]]
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return out
        after = page["endCursor"]


def describe(thread: dict) -> str:
    """One line naming a thread by what a reader recognizes it as, never by its id."""
    c = first_comment(thread)
    body = " ".join((c.get("body") or "").split())
    return (
        f"{thread.get('path')}:{thread.get('line')} "
        f"by {(c.get('author') or {}).get('login')}: {body[:120]}"
    )


def matching_threads(threads: list[dict], match: str, path: str | None) -> list[dict]:
    """Threads whose finding text contains `match`, narrowed by `path` where one is given.

    Matched on the finding's own words rather than on a line number, because a fix push moves the
    line and every lookup keyed to one then misses: replies posted against nothing while the
    resolves still succeeded, so the threads closed carrying no answer. Case-insensitive, since
    the text is quoted back out of a digest by a reader rather than compared by a machine.
    """
    needle = match.lower()
    return [
        t
        for t in threads
        if needle in (first_comment(t).get("body") or "").lower()
        and (path is None or t.get("path") == path)
    ]


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


def reply_to_thread(
    owner: str, repo: str, num: int, match: str, body: str, path: str | None, resolve: bool
) -> int:
    """Answer the one thread `match` selects, and resolve it where asked. Returns an exit code.

    Every refusal below is a stop rather than a fallback. There is no id to guess at, no
    second-best thread to settle for, and no resolve on a reply that did not land, because each
    of those closes a finding while leaving it unanswered, which is the state a reviewer reads as
    addressed.
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
            + ". Widen the words or drop --path rather than reaching for an id, since the "
            "thread may also be resolved already, which reads the same from here."
        )
        for t in threads:
            print(f"  unresolved: {describe(t)}")
        return 60
    if len(hits) > 1:
        print(
            f"status=AMBIGUOUS nothing was written: {len(hits)} unresolved threads carry "
            f"{match!r}, and picking one of them is the failure this avoids rather than a "
            "default it can take. Quote more of the finding, or add --path."
        )
        for t in hits:
            print(f"  candidate: {describe(t)}")
        return 61

    target = hits[0]
    print(f"answering: {describe(target)}")
    reply = (
        gh_graphql(M_REPLY, threadId=target["id"], body=body).get("addPullRequestReviewThreadReply")
        or {}
    )
    comment = reply.get("comment") or {}
    # The url is what says a reply carried a body, and an empty one still returns a comment.
    # Resolving past that closes the thread with nothing in it, which is what happened three times.
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
        print(
            "status=REPLIED the thread is answered and left open, since --resolve was not "
            "given. A decline is resolved only once its evidence is in the thread."
        )
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
    print("status=REPLIED_AND_RESOLVED")
    return 0


def gh_rest(path: str, jq: str | None = None) -> subprocess.CompletedProcess:
    """One REST read, returned whole so the caller can tell an absent object from an unread one.

    Unlike `gh_graphql` this does not raise on a non-zero exit, because a 404 here is an answer
    the caller acts on rather than a failure. Reads only: every path passed in is a GET.
    """
    argv = ["gh", "api", path] + (["--jq", jq] if jq else [])
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 1, "", "gh could not be run")


def answered_absent(proc: subprocess.CompletedProcess) -> bool:
    """Whether GitHub said the object is not there, as opposed to nothing having been read."""
    m = HTTP_STATUS.search(proc.stderr)
    return m is not None and m.group(1) in ABSENT


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
    """The empty string where `head` carries `sha`, `UNREAD` where nothing was read, else why not.

    Ancestry rather than membership of the branch's own commits, so a description may cite a
    commit it inherited from the base branch. GitHub reports the comparison from the base's side,
    so `identical` and `ahead` are the two readings that say the head carries it.
    """
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
        # Named as the description's problem rather than the branch's.
        # The branch is the ground truth here, and the body is what drifted away from it.
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["claims", "comment", "status", "reply", "wait"])
    ap.add_argument("number", type=int)
    # No default, because the wrong repository is the failure this argument has actually had.
    # A default names one repository, and every run from elsewhere silently reads that one.
    # The number resolves there, the digest renders, and nothing in the output disagrees.
    # Two runs read a pull request here while one in their own repository was the subject.
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
        help="wait: poll the full --timeout even where the reviewer's own most recent "
        "activity elsewhere in this repository is a quota-limit refusal with nothing "
        "answering it since, pass this once the quota is believed to have reset",
    )
    # `reply` takes the finding's words rather than its id.
    # There is deliberately no argument an id fits in, so the caller never holds one to mistype.
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
        "--body",
        metavar="TEXT",
        help="comment or reply: the answer to post, carrying the fixing commit SHA or "
        "the evidence that disproves the finding",
    )
    ap.add_argument(
        "--resolve",
        action="store_true",
        help="reply: resolve the thread once the reply is confirmed",
    )
    a = ap.parse_args(argv)
    # Named for the command they belong to, since one silently ignored reads as one that took effect.
    # A `status` given --body reports a clean digest and writes nothing.
    # Nothing in that output says the reply never happened.
    reply_only = {
        "--match": a.match,
        "--path": a.path,
        "--resolve": a.resolve or None,
    }
    if a.cmd != "reply":
        for flag, value in reply_only.items():
            if value is not None:
                ap.error(f"{flag} belongs to `reply`, not `{a.cmd}`")
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
    # A negative threshold reports every check in that state, on every run, from the first read.
    # A field that fires always is one a reader learns to skip, which costs the real case.
    for name in ("check_grace", "check_stall"):
        if getattr(a, name) < 0:
            ap.error(f"--{name.replace('_', '-')} cannot be negative")
    # The two thresholds mean opposite things and the readings invert if the order does.
    # A stall under the grace reports a running check before it would report a starved one.
    # A case held the constants ordered while the flags could still be passed either way.
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

    if a.cmd == "comment":
        return comment_on_pr(owner, repo, a.number, a.body)

    if a.cmd == "status":
        # One payload renders the digest and decides the code, for the reason `wait` reads one.
        # Fetched twice, a round landing between them prints one pull request and grades another.
        pr = gql(Q_FULL, owner, repo, a.number)
        out, _ = digest(owner, repo, a.number, pr=pr, grace=a.check_grace, stall=a.check_stall)
        print(out)
        return report_verdict(pr)

    if a.cmd == "reply":
        return reply_to_thread(owner, repo, a.number, a.match, a.body, a.path, a.resolve)

    # In-process backoff, so the whole wait costs one agent turn.
    delays = [15, 20, 30, 45, 60, 120]
    start = time.monotonic()
    pr = gql(Q_LIVE, owner, repo, a.number)
    done, answer = reviewed_head(pr), answered_outside_review(pr)
    # A drifted login matches no filter here, so `done` stays false however long this runs.
    # Waiting it out reports a review that landed as one that never did, at the timeout.
    # The liveness query carries the authors, so this costs the loop no extra call.
    drift = reviewer_login_drift(pr)
    # Read whenever nothing has landed on this pull request yet, whether or not a request is already outstanding.
    # An already-pending request drawing no answer at all is exactly the shape a repo-wide quota exhaustion leaves, per PR #962 and the six pull requests after it that carried no Copilot activity at all.
    # The bot id for a fresh request comes from this same traversal, so a caller needing either pays for one call rather than two.
    history = [] if done or answer or drift else copilot_history(owner, repo)
    # `--ignore-quota-signal` only changes whether the signal below is acted on.
    # It does not change whether the history is read, since the auto-request line still needs it for the bot id regardless.
    # Read from the same, unfiltered history rather than one that drops this pull request's own entries: a genuine review on an earlier head of this same pull request, superseded since by a push, is real evidence about the account and not a self-reference to discard.
    # A refusal on this pull request's own current head still never reaches this signal, since it is caught directly and at higher priority first.
    signal = None if a.ignore_quota_signal else quota_signal(history)
    # Request before the first poll, not just at the call site: a caller expects `wait` to make a review happen, not merely to watch for one.
    # Two prior gaps this closed, a push superseding an already-answered request and an auto-seed that never fired, both left nothing outstanding for the loop below to ever see land.
    # Skipped once a review already covers the head, once Copilot has already answered outside a formal review, or once something is already in the request set, so a second `wait` on the same PR never double-requests.
    if not done and not answer and not drift and not reviewer_requested(pr):
        print(f"auto-request: {request_copilot_review(pr['id'], copilot_bot_id(history))}")
        # No re-read here: Copilot never resolves within the round trip that just issued the request.
        # The loop below picks up fresh state on its own first iteration instead of this spending a second call to learn nothing new.
    if signal:
        # The poll below is skipped rather than shortened, because there is nothing partial about this signal.
        # The reviewer's own most recent word anywhere in the repository is the account quota, and nothing has answered it since.
        # Polling this pull request's own silence for up to 45 minutes would only relearn that same account state a call late.
        number, hist_refusal = signal
        print(
            f"note: the reviewer's own most recent activity anywhere in this repository, on "
            f"pull request #{number} at {hist_refusal.get('_at') or 'an unknown time'}, is a "
            "quota-limit refusal with nothing answering it since, so this wait stops here "
            "rather than polling --timeout out against the same account state. Pass "
            "--ignore-quota-signal to poll anyway, once the quota is believed to have reset."
        )
    else:
        i = 0
        while not done and not answer and not drift:
            elapsed = time.monotonic() - start
            if elapsed > a.timeout:
                break
            time.sleep(delays[min(i, len(delays) - 1)])
            i += 1
            # Re-read head each iteration: a push during the wait moves it.
            pr = gql(Q_LIVE, owner, repo, a.number)
            done, answer = reviewed_head(pr), answered_outside_review(pr)
            drift = reviewer_login_drift(pr)

    # One payload decides the digest and the exit code together.
    # Read separately, a review landing between them prints coverage and returns a timeout code.
    # A reader resolves that by believing the code, dropping the review it was just shown.
    # The digest also earns its call at the timeout.
    # A bare PENDING line reports a broken wait and a slow reviewer identically.
    final = gql(Q_FULL, owner, repo, a.number)
    now = datetime.now(UTC)
    # Parsed here and handed down, so the digest and the exit code share one read of the rollup.
    # Deriving the stuck shapes from that list costs no parse, which is what was doubled.
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
    # The shape reading comes first and is not gated on coverage of the head.
    # `reviewed_head` is itself one of the readings a drift breaks.
    # A renamed login matches no filter here, so it reads as no review at all.
    # Every arm below then reports a review that landed as a pending one.
    # Gating the verdict behind it left the login check unable to reach an exit code.
    # The digest above printed `shapes=UNRECOGNIZED` the whole time it did so.
    # Coverage of the head is the other half, returning 0 only once the diff is covered too.
    if unrecognized_shapes(final) or reviewed_head(final):
        verdict = report_verdict(final)
        # The check reading ranks under both of those, and never replaces either.
        # An unreadable shape means no field here can be believed, this one included.
        # A partial round means the diff is part-reviewed, which outranks a wedged gate.
        # Only once the review itself is sound does a stuck required check decide the code.
        if verdict:
            return verdict
        # The review loop closing is not the merge gate, and 0 alone was saying it was.
        # A wait ends the moment coverage lands, which leaves the checks mid-flight nearly always.
        # So a merely pending check is not this code, or the code would be the usual outcome.
        # Only a shape no waiting clears earns it, which is what the stuck field already prints.
        # It is read from the same payload the digest was, so the two can never disagree.
        # A rollup carries checks the ruleset does not require, four of six on a green run here.
        # So `BLOCKED` is required of the code as well, borrowing GitHub's own reading.
        # That is cheaper than reading the ruleset's contexts over another call.
        # Without it, a stuck check nothing requires returns 44 on a mergeable pull request.
        # `CLEAN` proves no required gate is outstanding, whatever else the rollup is doing.
        # The digest reports the check either way, so the narrower code costs the reader nothing.
        if stuck and final.get("mergeStateStatus") == "BLOCKED":
            # Worded as a coincidence rather than a cause.
            # Nothing here proves the stuck check is what blocks the merge.
            # `BLOCKED` is also worn by an open thread or a missing approval.
            # The rollup also carries checks no ruleset requires.
            # So naming the check as the blocker would assert a link this cannot read.
            # Both facts are true, and both are printed.
            print(
                "status=CHECKS_NOT_MERGEABLE the review loop is closed, the merge reads "
                "BLOCKED, and a check is in a shape waiting does not clear: read the block "
                "above, since a starved check wants a re-run, an unposted one its poster, a "
                "long one a judgment, and a failed one a fix. Which of them gates the merge is "
                "not read here, because BLOCKED is also worn by a thread or a missing approval"
            )
            return 44
        return 0
    # A refusal before an answer, since it names the round that declined where 40 names none.
    # The digest prints both bodies regardless, so the narrower code costs the reader nothing.
    refusal = refusing_review(final)
    if refusal and quota_refusal(refusal):
        print(
            "status=COPILOT_QUOTA_EXHAUSTED the review carrying the head declined because the "
            "requesting account has reached its Copilot review quota, printed above under "
            "COPILOT REFUSED THIS ROUND: that is an account-level state, not one this pull "
            "request or a re-request clears, so proceed on the coverage the other reviewers "
            "already gave this pull request rather than waiting on Copilot again"
        )
        return 46
    if refusal:
        print(
            "status=REVIEW_IS_A_REFUSAL the review carrying the head says it did not review, "
            "so it covers nothing and no further review follows it: read the body above, "
            "since a file-count refusal is cleared by splitting the pull request, and "
            "re-requesting this head clears nothing"
        )
        return 41
    # An answer before a stall, because the reviewer saying something outranks it saying nothing.
    if answered_outside_review(final):
        print(
            "status=ANSWERED_OUTSIDE_REVIEW the reviewer answered without reviewing, "
            "so read the comment above and decide, since where it declines or names a limit "
            "no review follows and re-requesting does not clear it"
        )
        return 40
    # Lowest priority of the terminal readings, since it is inferred from elsewhere in the repository rather than read on this pull request directly.
    # Any of the three above, being concrete evidence about this head, outranks it.
    if signal:
        number, hist_refusal = signal
        print(
            "status=COPILOT_QUOTA_EXHAUSTED_REPO_WIDE this pull request's head carries no "
            f"Copilot review or comment of its own, and the reviewer's own most recent activity "
            f"in this repository, on pull request #{number} at "
            f"{hist_refusal.get('_at') or 'an unknown time'}, was a refusal citing the "
            "account quota limit rather than a review of this head. Proceed on the coverage "
            "the other reviewers already gave this pull request, or pass --ignore-quota-signal "
            "to poll this pull request's own head for the full --timeout"
        )
        return 47
    print("status=PENDING")
    return 30


if __name__ == "__main__":
    sys.exit(main())
