# Copilot Instructions

Repository conventions for GitHub Copilot (and any other AI agent reading this file).

The **canonical guide is [AGENTS.md](../AGENTS.md)** at the repo root. Read it first, then the [PR Review Etiquette](../GOVERNANCE.md#pr-review-etiquette) review-loop contract this file's runbook implements. This file is intentionally narrow: commit/PR-title conventions (summarized inline so VS Code's commit-message and PR-title generators have them), guidance for reviewing carried fleet content, plus the GitHub Copilot Review Runbook.

For code-style rules, see [`CODESTYLE.md`](../CODESTYLE.md) at the repo root, one guide with a General section plus a section per language the repo uses.

Do not duplicate language-specific rules here. **Project-specific conventions and API/behavioral contracts also belong in [GOVERNANCE.md](../GOVERNANCE.md), not here.** This file is intentionally limited to the inline commit/PR-title summary, the guidance for reviewing carried fleet content, and the GitHub Copilot Review Runbook. Non-Copilot agents (Claude Code, Codex, Cursor, ...) are not directed to this file and don't read it by default, so any rule a reviewer must honor has to live in `GOVERNANCE.md`, routed to from `AGENTS.md`, to be provider-independent.

## Commit Messages and Pull Request Titles

Summarized for VS Code's generators. The full rules, rationale, and examples are in [GOVERNANCE.md "Pull Request Title and Commit Message Conventions"](../GOVERNANCE.md#pull-request-title-and-commit-message-conventions).

- Imperative subject, <= 72 characters, no trailing period, with an optional blank-line-separated body for the non-obvious *why*.
- US English, title case with lowercase short bind words. No vague titles, no `Co-Authored-By:` unless asked, no release-bump magnitude (NBGV handles versioning). Dependabot's `Bump X from Y to Z` titles are fine.
- develop PRs squash-merge (`gh pr merge --squash`), main PRs merge-commit (`--merge`). A mismatched flag is rejected by branch protection.

## Reviewing Carried Fleet Content

Several of this repository's governance files are carried from a shared template and kept in sync across a fleet of sibling repositories, among them `AGENTS.md`, `CODESTYLE.md`, `WORKFLOW.md`, this file, and the `repo-config/` rulesets. Most of `AGENTS.md` is universal fleet law: every section that states a rule, as opposed to the two that describe this repository's own directory tree and devcontainer, is byte-locked and verified by an automated byte-for-byte match against the template canonical, not by line-by-line review.

Two constraints follow when reviewing that content.

- **A reference inside byte-locked text to a path or section this repository does not carry is intentional, not a broken link.** Universal rule text names shared infrastructure (a fleet registry, a reusable config snippet, the other workflow model's ruleset payload) that a given repository legitimately may not contain. Editing the text to "fix" such a reference would break the fleet audit that governs it, so the reference is correct as written. Do not report it as a dead link, a missing file, or a broken cross-reference.
- **A genuine substantive defect is still worth raising.** Byte-locked is not unreviewable. A self-contradiction, a factual error, or a real typo in the canonical prose is a valid finding, but note that the fix lands at the template and re-vendors to every repository, rather than proposing a local edit the audit would reject.

## GitHub Copilot Review Runbook

> This runbook implements the [GOVERNANCE.md "PR Review Etiquette"](../GOVERNANCE.md#pr-review-etiquette) review-loop contract for GitHub Copilot. Without it in-repo, an agent has no pointer to the reliable Copilot mechanics and falls back to known-broken paths (the no-op `POST /requested_reviewers`, the wrong bot-login filter). In the API snippets below, fill the `<owner>` / `<repo>` / `<N>` placeholders.

Use this section for provider-specific mechanics. The expected review loop *contract* (request review on every push, verify head-SHA coverage, triage findings, reply + resolve, escalate when stuck) is defined in [GOVERNANCE.md -> PR Review Etiquette](../GOVERNANCE.md#pr-review-etiquette). This section only describes how to make GitHub Copilot reliably execute it.

### Triggering and Polling

Auto-review on push is configured (via the branch ruleset's `copilot_code_review` rule with `review_on_push: true`) but fires inconsistently in practice, so treat it as best-effort, not guaranteed. After every push, **re-request a review programmatically** via the GraphQL `requestReviews` mutation, passing the Copilot reviewer's bot node id in `botIds`. This drives the loop end-to-end without a UI hand-off.

**A review with no inline comments is still a completed review, not a failure, and not a reason to ask the maintainer to re-trigger.** Copilot very often posts a single formal review (GraphQL `state: COMMENTED`) whose body ends with "...reviewed N of N changed files ... and generated no comments" and adds **zero** inline threads. That review carries the head `commit.oid` and fully satisfies the loop, and it is the clean-pass success case. Never read "no inline comments" as "the review didn't run," and never re-request or escalate to the maintainer because comments are absent.

**Read the low-confidence findings, which are not inline threads.** A review body can carry a collapsed `<details>` block of findings Copilot withheld from the inline threads, and those findings appear nowhere in `reviewThreads`, so a loop that polls threads alone never sees them and reports a clean pass. **Match the block on more than one phrasing.** Its heading has appeared both as `Suppressed comments (N)` and as "Comments suppressed due to low confidence", so a filter keyed on either one alone silently reports zero suppressed findings on a review that has them, the same false clean this rule exists to prevent, one level up in the detection. They have been right repeatedly, including a rule stated more broadly than its check enforced and a check that skipped fenced blocks in every rule but one. Read the body of every review, investigate each suppressed finding on the same footing as an inline one, and answer it in the PR conversation, since a suppressed finding has no thread to reply on or resolve.

```sh
# `test` with an alternation, not `contains` on one phrasing: the heading wording has changed.
gh api repos/<owner>/<repo>/pulls/<N>/reviews --jq \
  '.[] | select(.body | test("Suppressed comments|low confidence")) | .body'

# Scope it to the current head, so an answered finding from an earlier round does not re-open.
PR_HEAD=$(gh pr view <N> --json headRefOid --jq '.headRefOid')
gh api repos/<owner>/<repo>/pulls/<N>/reviews --jq \
  "[.[] | select(.commit_id==\"$PR_HEAD\") | select(.body | test(\"Suppressed comments|low confidence\"))] | length"
```

**Round 1 is normally auto-seeded, so poll for it before trying to self-trigger.** Auto-review-on-open supplies the first review with no `botIds` call needed, but it can lag one to three minutes. After opening a PR (or the first push), **poll** for a Copilot review on the head SHA (see [Verify Review Covered Current Head](#verify-review-covered-current-head)) before concluding none ran. The `requestReviews` mutation below is for **re-requesting on later pushes** (a new head SHA). By then a prior review exists, so its bot node id is readable. A missing bot node id on round 1 therefore means "the auto-review has not landed yet - wait and poll," **not** "ask the maintainer to kick it off."

> **The reviewer login differs by API.** In **GraphQL** (`gh api graphql` and `gh pr view --json reviews`, which is GraphQL-backed) the `Bot.login` is `copilot-pull-request-reviewer`, with **no `[bot]` suffix**. In the **REST** API (`gh api repos/.../issues|pulls/...`) the same account's `user.login` is `copilot-pull-request-reviewer[bot]`, **with** the suffix. Each query below uses the correct form for its API, so match the API, not a single spelling, when adapting them.

```sh
# 1. PR node id + the Copilot reviewer's bot node id (read from any existing
#    Copilot review; the reviewer login is `copilot-pull-request-reviewer`).
PR_NODE=$(gh pr view <N> --json id --jq '.id')
BOT_ID=$(gh api graphql -f query='
{
  repository(owner: "<owner>", name: "<repo>") {
    pullRequest(number: <N>) {
      reviews(first: 50) { nodes { author { __typename login ... on Bot { id } } } }
    }
  }
}' --jq '[.data.repository.pullRequest.reviews.nodes[]
          | select(.author.login == "copilot-pull-request-reviewer")
          | .author.id] | first')

# 2. Re-request a Copilot review on the current head.
gh api graphql -f query='
mutation($pr: ID!, $bot: ID!) {
  requestReviews(input: { pullRequestId: $pr, botIds: [$bot], union: true }) {
    pullRequest { id }
  }
}' -F pr="$PR_NODE" -F bot="$BOT_ID"
```

The bot node id is read from an existing Copilot **formal** review (`pullRequest.reviews`), so step 1 needs at least one prior formal review on the PR, and the auto-review-on-open normally supplies the first one (it may have **no inline comments**, which still counts, and its bot node id is still readable). Poll for it (give auto-review-on-open a few minutes) before deciding it is missing.

**Cold start (round 1 not yet landed): read the id repo-wide, not from this PR.** The Copilot reviewer's bot node id is the reviewer bot *account's* node id and is **stable across every PR in the repo**. So a freshly opened PR that has neither a formal review nor an issue comment yet does **not** need UI seeding to bootstrap the id: read it from any prior Copilot review anywhere in the repo, then feed it into the `requestReviews` mutation to drive round 1. Query the **most recent** PRs (`first: 20` with an explicit newest-first order; plain `last: 20` returns the *oldest* PRs, which may predate Copilot on the repo), and **guard for an empty result**, since an empty `$BOT_ID` means none of the sampled PRs carry a Copilot review. Widen the window (raise the count or paginate) before concluding the repo has never had one and falling back to UI seeding; never feed an empty id into the mutation:

```sh
BOT_ID=$(gh api graphql -f query='
{
  repository(owner: "<owner>", name: "<repo>") {
    pullRequests(first: 20, orderBy: { field: CREATED_AT, direction: DESC }) {
      nodes { reviews(first: 20) { nodes { author { __typename login ... on Bot { id } } } } }
    }
  }
}' --jq '[.data.repository.pullRequests.nodes[].reviews.nodes[]
          | select(.author.login == "copilot-pull-request-reviewer")
          | .author.id] | first // empty')
if [ -z "$BOT_ID" ]; then
  echo "no Copilot review in the 20 most recent PRs - widen the window, else fall back to UI seeding" >&2
  return 1 2>/dev/null || exit 1   # stop; do NOT call requestReviews with an empty id
fi
```

If Copilot posted **only an issue comment** on this PR and no formal review, you can instead read the id from that comment's author (`pullRequest.comments` -> author `... on Bot { id }`). Manual UI seeding is the last resort, needed only for a repo that has **never** had a Copilot review, so no prior id exists anywhere to read. Use the mutation for every subsequent re-request.

**Do NOT post `@Copilot review` as a PR comment.** That comment triggers the Copilot *coding agent* (`copilot-swe-agent[bot]`), which makes code changes rather than posting a review.

Known non-working request paths (don't rely on them, and use the `requestReviews` mutation above instead):

- `POST /requested_reviewers` with `reviewers=[Copilot]` can return 200 but no-op.
- `copilot-pull-request-reviewer` as a requested reviewer slug returns 422.
- `requestReviews` with the reviewer's bot node id in **`userIds`** fails with `Could not resolve to User node`, because the Copilot reviewer is a **Bot**, so its node id goes in **`botIds`** (as in the mutation above), never `userIds`.
- `suggestedActors(capabilities: [CAN_BE_ASSIGNED])` lists `copilot-swe-agent` (the coding agent), not `copilot-pull-request-reviewer`, so do not source the reviewer's bot node id there. Read it from an existing review per step 1 above.
- There is no `removePullRequestFromReviewRequest` mutation, and removing the reviewer to force a fresh pass is unnecessary anyway, since `requestReviews` with `union: true` re-fires the review on the current head.

### Verify Review Covered Current Head

Before merging, confirm Copilot reviewed the current PR head SHA. Copilot may respond as either a formal review (carries an exact commit SHA) or an issue comment (no SHA, so use the most recent Copilot comment for manual confirmation). Check both.

**Count matches and compare numerically, so an empty result cannot read as success.** A poll that captures a `gh api --jq` result and exits on `[ "$found" != "0" ]` treats an **empty** string as a landed review, and an empty string is exactly what a mis-written filter returns. Pipe the matches through `wc -l` and test `-gt 0`, so a query that finds nothing and a query that ran wrong both read as "not yet". A `gh` call that fails to run reaches the test the same way, because it writes its message to stderr and prints nothing to stdout, so the `$(...)` around it still yields the empty string. A mistyped or unsupported flag is the usual cause, and `gh` reports one as `accepts 1 arg(s), received 4` rather than as anything resembling a review verdict.

**Check head coverage before reading merge-state, never the reverse.** A push makes the required checks go green before Copilot re-reviews the new head, so `mergeStateStatus` can read `CLEAN` in the window before any formal review covers the head. A poll that exits on `CLEAN` merges into that gap. Gate on a formal review whose `commit.oid` equals the current head SHA first, then on zero unresolved threads, and only then read merge-state.

```sh
PR_HEAD=$(gh pr view <N> --json headRefOid --jq '.headRefOid')

# 1. Formal review - exact SHA match.
gh pr view <N> --json reviews --jq \
  '.reviews[] | select(.author.login=="copilot-pull-request-reviewer") | .commit.oid' \
  | grep -q "$PR_HEAD" && echo "covered via formal review"

# 2. Issue comment - show the most recent Copilot comment for manual
#    confirmation. This is the REST API, so the login carries the `[bot]` suffix.
gh api repos/<owner>/<repo>/issues/<N>/comments --jq \
  '[.[] | select(.user.login=="copilot-pull-request-reviewer[bot]")] | last | {created_at, body: .body[:200]}'
```

Coverage is confirmed when (1) exits 0, and **a formal review with no inline comments still satisfies path (1)**, because coverage is about the head SHA, not the comment count. For issue comments (path 2), body content is the only reliable signal, and `created_at` is not: `git log -1 --format=%cI` is the **commit** timestamp, not the push timestamp, so amended or rebased commits can have an earlier timestamp and an older Copilot comment could satisfy a time check even though Copilot never saw the current head. Treat path (2) as confirmed only when the comment body explicitly refers to the current changes.

### Bounded Retry Workflow

This path is only for a **genuinely missing** review, meaning no Copilot review (formal *or* issue comment) covers the current head SHA after polling. A review that covered the head but produced no comments is a clean pass, not a missing review, so do not enter this retry path for it.

**A slow review is pending, not missing, so poll with backoff and never escalate on a timeout alone.** Copilot can lag far beyond the usual one-to-three minutes when it has been re-requested many times in quick succession, because it throttles under load, and a re-review landing tens of minutes after the request is normal. A poll that times out is therefore evidence only that the review has not landed *yet*, not that Copilot is done or unresponsive. Report the status as "review still pending" and keep polling on a widening interval (for example 20s steps, then a few minutes) rather than stopping. Enter the escalation step below only when the `requestReviews` mutation itself no-ops or errors, or after a genuinely long wait with the request confirmed accepted, never merely because one fixed poll window elapsed.

If a review did not run on the current head, retry:

1. Wait briefly and check head-SHA coverage (see above).
1. Re-request the review via the `requestReviews` mutation (see "Triggering and Polling"), falling back to the GitHub PR UI only if the mutation no-ops.
1. Retry up to two more times (three total).
1. If still missing, mark review as blocked and escalate to the user/maintainer with what was attempted.

### Reply and Thread Resolution Workflow

Every id below is captured from a live query into a variable and passed from there, never hand-typed, guessed, or pasted as a `PRRT_...` literal. A node id resolves globally, so a fabricated or stale id does not fail, it writes to a real thread on an unrelated repository. This runbook implements [GOVERNANCE.md "Repository Boundaries and Write Safety"](../GOVERNANCE.md#repository-boundaries-and-write-safety): write only to this repo, capture every id from a live query, and never suppress a mutation's output.

List unresolved threads. Use `first: 100` with cursor-based pagination, and where `hasNextPage` is true, re-run with `after: "<endCursor>"` to retrieve the next page:

```sh
gh api graphql -f query='
{
  repository(owner: "<owner>", name: "<repo>") {
    pullRequest(number: <N>) {
      reviewThreads(first: 100) {
        nodes {
          id isResolved path
          comments(first: 1) { nodes { author { login } body } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}' | jq '
  .data.repository.pullRequest.reviewThreads |
  (.pageInfo | "hasNextPage=\(.hasNextPage) endCursor=\(.endCursor)"),
  (.nodes[] | select(.isResolved == false))
'
```

Reply on a thread, then resolve it. Capture the target thread's id into `$TID` from the listing query above, filtering to the thread being answered by its `path`, and guard for an empty result so a mutation never runs on a guessed id. When a file carries more than one unresolved thread, `path` alone is ambiguous and `head -n 1` would pick the wrong one, so narrow by first-comment body (the query already fetches `comments(first: 1)` for this) by adding `and (.comments.nodes[0].body | contains("<SNIPPET>"))` to the `select`:

```sh
TID=$(gh api graphql -f query='
{
  repository(owner: "<owner>", name: "<repo>") {
    pullRequest(number: <N>) {
      reviewThreads(first: 100) {
        nodes { id isResolved path comments(first: 1) { nodes { body } } }
      }
    }
  }
}' --jq '.data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved == false and .path == "<PATH>")
  | .id' | head -n 1)
[ -n "$TID" ] || { echo "no matching unresolved thread on <PATH> - do not guess an id" >&2; return 1 2>/dev/null || exit 1; }

# Show the mutation's output. Never append an output-discard or force-success tail
# (>/dev/null, 2>/dev/null, &>/dev/null, || true, || :, || echo) to a write.
gh api graphql -f query='
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: { pullRequestReviewThreadId: $threadId, body: $body }) {
    comment { id url }
  }
}' -F threadId="$TID" -F body="Fixed in <SHA>: <one-line summary>."

# Confirm isResolved: true in this response before treating the thread as closed - a write that
# appears to fail may have taken on the server.
gh api graphql -f query='
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) { thread { id isResolved } }
}' -F threadId="$TID"
```

Issue-level Copilot comments (those in `issues/<N>/comments`) have no resolution action, since GitHub provides no API or UI to resolve them. Reply if the finding warrants it, but no resolution step is needed or possible.

### PR Edits and Merge-State Gotchas

- **`gh pr edit --title/--body` is broken here.** It touches the deprecated Projects-classic `projectCards` GraphQL field and **exits non-zero without applying the change** (a stale PR description then survives review rounds). Edit the title/body via the API and verify it took: GraphQL `updatePullRequest(input: { pullRequestId, title, body })`, or REST `gh api -X PATCH repos/<owner>/<repo>/pulls/<N> -F body=@body.md` (the `@` reads the body from a file, so name it explicitly, not the literal `file`).
- **`main`/`develop` use rulesets, not classic branch protection.** The classic protection REST endpoint (`repos/.../branches/<b>/protection`) 404s, so read the ruleset instead. A `mergeStateStatus` of `BLOCKED` on a green PR is usually just **unresolved review threads** (the ruleset requires thread resolution); resolving them moves it to `CLEAN`. (`BLOCKED` is a `mergeStateStatus` value; don't confuse it with the separate `mergeable` field's `MERGEABLE`/`CONFLICTING`, which reports merge conflicts, not review gates.)
- **Push -> head-SHA read race.** A `headRefOid` read taken immediately after a push can return the **old** head, so re-read after the push registers, or a coverage poll evaluates the stale SHA.
- **Copilot is sometimes factually wrong** (e.g. it claimed `actionlint -color` "requires a value" when it is a boolean flag). Verify a finding before fixing, and decline with evidence when it is wrong, which is distinct from dismissing a still-present finding as stale.

Reply-body conventions:

- Accepted bug/style fix: include fixing commit SHA and a one-line summary.
- Declined style comment: cite the rule (GOVERNANCE.md or the CODESTYLE.md language section) and the existing-tree precedent.
- Declined architecture proposal: one-sentence rationale.
- Declined false positive on carried fleet content (a broken-link or dead-cross-reference flag inside byte-locked rule text): cite the "Reviewing Carried Fleet Content" section, since the reference is intentional and the text cannot be edited locally.

After the final push, sweep-resolve stale older threads for removed code paths.

## When in Doubt

Read [AGENTS.md](../AGENTS.md) to find the section that governs your change, and [GOVERNANCE.md](../GOVERNANCE.md) for the rule text itself. For code-style rules, [`CODESTYLE.md`](../CODESTYLE.md) (its General section plus the relevant language section) is authoritative. Don't restate any of these files' rules in commit bodies or PR descriptions, and keep those focused on the change itself.

If you find a gap in the governance itself (this file, AGENTS.md, or GOVERNANCE.md is out of date, a rule is missing, something bit this repo and would bite the next), fix it in the governance docs as part of your change rather than only working around it locally.
