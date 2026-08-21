# Pull Request Reviewer Evaluation

This document measures whether additional automated reviewers improve the fleet's pull request review loop enough to justify their noise and operating cost.

## Table of Contents <!-- omit from toc -->

- [Status](#status)
- [Evaluation Method](#evaluation-method)
- [Current Assessment](#current-assessment)
- [Finding Log](#finding-log)
- [Interaction and Operations](#interaction-and-operations)
- [Plan and Repository Scope](#plan-and-repository-scope)
- [First-Class Support Criteria](#first-class-support-criteria)
- [Future `pr_review.py` Support](#future-pr_reviewpy-support)
- [Next Evaluation Steps](#next-evaluation-steps)

## Status

**State:** Active evaluation\
**Incumbent:** GitHub Copilot\
**Candidates:** CodeRabbit and Qodo\
**Samples:** [ProjectTemplate pull request #891][pr-891], [pull request #892][pr-892], and [pull request #893][pr-893]

No candidate is a required reviewer. A candidate remains advisory until it meets the first-class support criteria below.

## Evaluation Method

Each finding receives one disposition after verification against the current head, repository rules, and relevant primary documentation.

| Disposition | Meaning |
| --- | --- |
| True positive | The reported behavior or design gap is real and the finding's central claim is correct |
| Mixed | A real issue is present, but the finding overstates its scope or recommends an unsupported remedy |
| False positive | The claimed issue is contradicted by code, policy, or product behavior |
| Duplicate | Another reviewer already reported the same root cause on the same head |

The evaluation records duplicates separately from correctness. A correct duplicate has less marginal value than the first report, but it still measures reviewer accuracy.

The assessment also records:

- Changed-file and current-head coverage.
- Time until a terminal review result.
- Whether the result clearly distinguishes findings from success.
- Inline-comment quality and smallest useful line anchors.
- Ease of replying, resolving, and re-requesting review.
- Stability of reviewer identity and output structure.
- Availability, rate limits, repository visibility limits, and plan changes.
- Data access and retention terms that affect private repositories.

## Current Assessment

The first sample is too small for an adoption decision. It does show that both candidates find real issues while producing different kinds of review noise.

| Reviewer | True Positive | Mixed | False Positive | Duplicate Roots | Initial Reading |
| --- | ---: | ---: | ---: | ---: | --- |
| GitHub Copilot | Not scored | Not scored | Not scored | Not scored | At least seven review attempts across two pull requests ended in an error, so no review covered any reviewed head |
| CodeRabbit | 25 | 2 | 0 | 3 | Strong issue discovery, with occasional remedies that overstate configuration scope or contradict another valid control |
| Qodo | 3 | 1 | 4 | 2 | Finds additional reliability issues, but repeatedly misreads repository title rules and documented tool behavior |

The duplicate roots were target-discovery failure, markdownlint filename handling, and process-failure classification. Qodo alone raised command-line length limits. CodeRabbit alone covered the postponed runner design, Docker mount quoting, and the editor-extension identifier.

These counts judge central claims, not every sentence in a comment. The detailed finding log preserves qualifications that the table cannot express.

## Finding Log

### 2026-08-21: Pull Request #891

CodeRabbit posted nine actionable findings:

- Eight true positives: Copilot runner isolation, Dependabot isolation, runner-group authorization, bounded offline testing, the editor identifier, target-discovery failure, Docker mount quoting, and markdownlint literal filenames.
- One mixed finding: rollback must cancel homelab work, but the repository variable is authoritative here rather than an organization variable.
- Two findings duplicated roots Qodo also reported.

Qodo posted five findings:

- Two true positives: target-discovery failure and command-line length limits.
- One mixed finding: markdownlint needs literal filename handling, but the comment did not establish its broader claim for every linter.
- Two false positives: `to` is an allowed lowercase title bind word, and the Docker-lint documentation follows the intended skill and runbook ownership model.
- Two findings duplicated roots CodeRabbit also reported.

Copilot posted three terminal error responses and no findings. This sample therefore measures candidate value during an incumbent outage, not comparative recall over the same completed review.

After `develop` advanced, CodeRabbit added two true positives: the organization Copilot runner policy also governs the cloud agent, and homelab labels must not overlap the hosted selector. Qodo added no finding.

### 2026-08-21: Pull Request #892

Qodo posted three findings:

- One true positive: a generic process-start failure was labeled as a timeout.
- Two false positives: `PR` uses the correct acronym casing, and markdownlint-cli2 documents `--` as making the remaining arguments literal.

CodeRabbit posted seven findings:

- Six true positives: public-runner opt-in enforcement, a protected-ref canary, precise Docker-runner documentation, accurate Copilot-loop wording, timeout-cleanup failure handling, and ShellCheck option termination.
- One mixed finding: jobs must select the restricted runner group explicitly, but removing the selected-workflow restriction would weaken the valid protected-ref design.
- One finding duplicated Qodo's process-failure classification root and extended it to timeout cleanup.

CodeRabbit skipped the draft, then skipped the ready pull request because its base was `develop` rather than the default branch. The review required an explicit `@coderabbitai review` command and completed in 7 minutes 37 seconds.

Qodo completed 2 minutes 42 seconds after the pull request became ready.

After the corrective push, CodeRabbit completed an explicitly triggered incremental review in exactly 8 minutes and added no findings. Qodo updated its existing review comment in place, marked the corrected process-failure finding resolved, and added no findings. Neither follow-up created a new formal review object, so provider-aware automation must also recognize updated comments and replies that report command completion.

Successive documentation-only follow-ups found two true positives: a compound-modifier error and inconsistent reviewer-attempt counts. The later comment that identified the resulting log-total mismatch repeated the count-reconciliation root. Qodo added no finding.

Later follow-ups found six more true positives: upstream draft handoff, live runner authorization evidence, duplicate-root bookkeeping, the active workflow-restriction flag, the exact repository set, and the exact workflow set.

Copilot posted terminal error responses on at least four successive pull request heads and supplied no review coverage.

### 2026-08-21: Pull Request #893

All three reviewers reported no findings. Qodo completed in 7 seconds, Copilot completed in 1 minute 55 seconds, and an explicitly triggered CodeRabbit review completed in 4 minutes 36 seconds.

Copilot reviewed 2/2 changed files at Lite effort. Its new `Approval recommended` heading was not recognized by `pr_review.py`, so the otherwise clean review remained blocked. [Issue #894][issue-894] records the shape, and the pull request adds a regression fixture for it.

On the parser-fix head, Copilot reviewed 4/4 files with no findings and the updated script recognized its output. CodeRabbit found one true-positive stale corpus count. Qodo did not reprocess the second push automatically.

## Interaction and Operations

### GitHub Copilot

The repository already has first-class status, wait, comment, reply, resolution, coverage, and output-shape handling in `scripts/pr_review.py`.

The current weakness is availability. A terminal error can leave the required review loop without coverage even when CI is green.

### CodeRabbit

The review body provides an actionable summary and links each finding to an inline thread. This makes manual triage straightforward.

Automatic review skipped a feature-to-`develop` pull request because `develop` is not the repository default. The review loop must explicitly trigger CodeRabbit unless its configuration changes.

Incremental follow-up also requires an explicit command on this pull request. Completion is reported by updating the command reply rather than by creating a new formal review.

The collapsed analysis is verbose and can dominate API output. Machine support should read normalized summaries and thread metadata without loading the analysis transcript.

Its status context reported success while nine actionable comments remained. A future gate must derive finding state from review threads, not from that status alone.

### Qodo

The findings are individually anchored and usually concise after HTML presentation is removed.

The formal review body was empty. All useful state lived in inline comments, so a body-only reader would report no findings.

After a corrective push, Qodo updated the existing review comment and its resolved state rather than creating another review. A future adapter must compare comment updates and finding dispositions, not only review creation events.

The first sample shows more policy false positives than CodeRabbit. It also supplied the only command-line length finding, which gives it measurable incremental value.

## Plan and Repository Scope

The maintainer intends to leave the paid trial when it expires and use only an available no-cost open-source tier. Candidate use is therefore limited to public repositories unless the maintainer approves a later plan change.

[CodeRabbit's current plan documentation][coderabbit-plans] provides an open-source tier for public repositories with rate limits. Confirm its terms again when the trial ends because product plans are external state.

Qodo remains under evaluation. Confirm its current public-repository availability, limits, and required permissions before relying on it outside this repository. Its [code-review documentation][qodo-review] describes the review product but does not settle the fleet's plan decision.

Private repositories remain Copilot-only unless a candidate's approved plan, data terms, and GitHub App permissions receive a separate review.

## First-Class Support Criteria

A reviewer becomes first-class only after all of these are true:

- At least ten pull requests and 30 findings have verified dispositions across Markdown, Python, workflow, and code changes.
- True positives materially exceed false positives, and mixed findings do not require repeated policy correction.
- The reviewer provides a detectable terminal result tied to the current head commit.
- Changed-file coverage is measurable, or the integration reports that coverage is unknown.
- Every finding can be enumerated, replied to, and resolved without scraping rendered HTML.
- A success check cannot hide actionable findings.
- Rate limits and repository-visibility restrictions fail visibly.
- Reviewer identity and output shapes are stable enough for fixture-based tests.
- The reviewer adds unique true positives often enough to justify extra review latency and triage.

No reviewer becomes a required merge gate solely because it is installed. The merge gate changes only after the measured evidence supports that decision.

## Future `pr_review.py` Support

The script remains Copilot-specific during this evaluation. Expand it only after a candidate meets the first-class support criteria.

The expansion should preserve one compact command while adding provider adapters behind a normalized review model:

- Provider identity and current-head review matching.
- Terminal, pending, failed, and rate-limited states.
- Review-body, inline-thread, and body-only findings.
- Thread replies and resolution where the provider uses GitHub review threads.
- Provider-specific status contexts that never substitute for finding enumeration.
- Changed-file coverage and an explicit unknown state where a provider supplies none.
- Stable output-shape detection with captured fixtures for each provider.
- Per-provider request behavior, with no automatic re-request until its idempotence is proven.

`status` should report every enabled reviewer on one line and then list unresolved findings grouped by provider. `wait` should finish only when each selected reviewer reaches a recognized terminal state.

The existing Copilot adapter remains behaviorally unchanged during extraction. Provider support must not weaken its refusal, partial-coverage, suppressed-finding, or unrecognized-shape checks.

## Next Evaluation Steps

1. Record every CodeRabbit and Qodo finding on subsequent public pull requests.
2. Measure time to review, current-head coverage, duplicates, and interaction effort.
3. Recheck candidate plan terms when the CodeRabbit trial expires.
4. Decide whether either candidate meets the first-class support criteria.
5. Design `pr_review.py` provider adapters only for candidates that graduate.
6. Decide separately whether a graduated reviewer is advisory or required.

<!-- GitHub -->

[pr-891]: https://github.com/ptr727/ProjectTemplate/pull/891
[pr-892]: https://github.com/ptr727/ProjectTemplate/pull/892
[pr-893]: https://github.com/ptr727/ProjectTemplate/pull/893

<!-- Issues -->

[issue-894]: https://github.com/ptr727/ProjectTemplate/issues/894

<!-- External -->

[coderabbit-plans]: https://docs.coderabbit.ai/management/plans
[qodo-review]: https://docs.qodo.ai/code-review
