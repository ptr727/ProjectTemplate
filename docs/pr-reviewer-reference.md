# Pull Request Reviewer Reference

This document is the operational reference for how each pull request reviewer in this fleet behaves: which repositories it covers, how it is triggered, what its output looks like, and which committed file shapes it. It answers "what will this reviewer do here?", separately from [`pr-reviewer-evaluation.md`][pr-reviewer-evaluation], which measures whether a candidate is worth keeping and changes as that measurement changes.

The split is deliberate. A reader mid-review needs the behavior and needs it to be stable. The evaluation is a living argument and moves for reasons that have nothing to do with the behavior.

Much of what follows is external product state that the fleet observes rather than sets, so it goes stale without warning. An observation disagreeing with this file is not always that, though: a skipped review also comes from a rate limit, or from a repository's own committed configuration, both described below. Rule those out first, and where neither explains it, the product moved and this file is what gets corrected.

## Table of Contents <!-- omit from toc -->

- [Coverage](#coverage)
- [Interaction and Operations](#interaction-and-operations)
- [Plan and Repository Scope](#plan-and-repository-scope)

## Coverage

As of September 2026 both candidates run on their open-source tiers. CodeRabbit and Qodo review the maintainer's public repositories and never a private one, so a private repository has Copilot as its only pull request reviewer, and Copilot's own review budget, a self-configured premium request cap, runs out under concurrent pull requests. CodeRabbit's open-source tier auto-reviews only a repository with at least ten stars, so on `ProjectTemplate`, which holds fewer, it reviews only on an explicit trigger. The Claude GitHub App is installed on the account and is not configured as a reviewer, since the `local-strict-review` Skill already runs a review pass before every push toward a pull request.

The consequence a review loop actually needs: **a private repository has Copilot alone**, and no trigger produces a candidate review there, while **a public repository below CodeRabbit's star gate still has a CodeRabbit review for the asking**. `.agents/skills/pr-review-conduct/SKILL.md` "Which Reviewers a Repository Actually Has" states the rule an agent applies, in a form that needs none of the facts above, since it is carried into repositories that hold no copy of this file.

## Interaction and Operations

### GitHub Copilot

The repository already has first-class status, wait, comment, reply, resolution, coverage, and output-shape handling in `scripts/pr_review.py`.

### CodeRabbit

The review body provides an actionable summary and links each finding to an inline thread. This makes manual triage straightforward.

Automatic review skips a pull request whose base is not the repository default unless [`.coderabbit.yaml`][coderabbit-auto-review] lists the base under `reviews.auto_review.base_branches`, which the hub's file does for `develop`. The default branch is always included, and each entry is a regex. On the open-source tier, automatic review also needs the repository to hold at least ten stars, which `ProjectTemplate` does not, so a review here is triggered by commenting `@coderabbitai review`, and until then CodeRabbit's summary comment says so in place of a review while its status check reports success.

Its collapsed analysis is verbose and can dominate API output, and its status context reports success while actionable comments remain, so finding state is read from review threads rather than from that check.

On the open-source tier it is also rate limited. A rate-limited pull request receives a notice naming when a review can next run, in place of the review, and it has the same shape as the star-gate skip notice while behaving oppositely: re-triggering returns the notice again rather than a review.

Incremental follow-up needed an explicit command on [pull request #892][pr-892]. Completion is reported by updating the command reply rather than by creating a new formal review.

### Qodo

The findings are individually anchored and usually concise after HTML presentation is removed.

After a corrective push it updates the existing review comment and its resolved state rather than creating another review, so a reader comparing review creation events alone sees no second round.

The formal review body was empty. All useful state lived in inline comments, so a body-only reader would report no findings.

### Generated Mirrors

`.github/skills/` and `.claude-plugin/fleet-skills/` are the trees `scripts/build_dist.py` generates from `.agents/skills/`, and CI holds them current, so a finding in either belongs at its source and a review of every copy is one finding three times. Two committed files tell CodeRabbit and Qodo to skip them, and Copilot's exclusion is a repository setting this account does not have, so `.github/copilot-instructions.md` asks instead.

- **CodeRabbit** reads `reviews.path_filters` from [`.coderabbit.yaml`][coderabbit-config] at the repository root, where a pattern prefixed with `!` excludes.
- **Qodo** reads [`.pr_agent.toml`][qodo-config] from the root of the default branch, so the file binds only once it is promoted to `main`, and its [`[ignore]` glob list][qodo-ignore] names the paths to skip.
- **GitHub Copilot** honors [content exclusion][copilot-exclusion], a repository setting under Copilot rather than a file in the tree, whose paths are `fnmatch` patterns, anchored to the repository root by a leading slash and matched anywhere without one. GitHub documents the setting for organizations on a Business or Enterprise plan, and this repository is under a user account, so it is unavailable here. In its place, `.github/copilot-instructions.md` "Reviewing Carried Fleet Content" asks Copilot to post no comment on either tree, and GitHub's [code review customization tutorial][copilot-customize] documents instruction compliance as non-deterministic, so an instruction may be overlooked where a setting cannot.

### Review Configuration

Each reviewer's behavior is shaped by a committed file rather than accepted as given, and each setting below carries the finding or the cost that earned it.

- **CodeRabbit**, in [`.coderabbit.yaml`][coderabbit-config]: auto review on pull requests into `develop`, which the open-source tier honors only at ten stars or more, no pause after five reviewed commits, since a fleet pull request routinely passes five pushes and the pause reads as a reviewer that stopped. A path instruction for Markdown asks for false, stale, unverifiable, or unfollowable claims only, since CI lints style and the local review pass reads canonical prose whole. Sequence diagrams, suggested labels and reviewers, and the in-progress fortune are off. The markdownlint, actionlint, shellcheck, and ruff tools are off, since CI runs the same four and fails the pull request on them.
- **Qodo**, in [`.pr_agent.toml`][qodo-config]: an issues guideline asks for a reproduction with any claimed crash, after a claimed `IsADirectoryError` on this repository's build was disproven by running it. A compliance guideline asks for the rule's own sentence and routes a rule against unchanged text to the summary. Informational findings go to the [summary][qodo-verbosity] rather than a thread, since a thread blocks the merge until resolved. Images are off so a finding's title is plain text a matcher can see. Qodo's [review standards][qodo-rules] import from `AGENTS.md`, `CLAUDE.md`, `copilot-instructions.md`, and `SKILL.md` files, each scoped to its folder at any depth, when changes merge, and only new rules are added, so an edited or deleted rule is changed in its portal instead.
- **GitHub Copilot**: the carried `.github/copilot-instructions.md`, which bootstraps the `code-review` Skill, is the lever this repository uses. GitHub also documents path-scoped `.github/instructions/*.instructions.md` files, unused here.

## Plan and Repository Scope

The paid trials are over, and both candidates run on their no-cost open-source tiers. Candidate use is therefore limited to public repositories unless the maintainer approves a later plan change.

[CodeRabbit's current plan documentation][coderabbit-plans] provides an open-source tier for public repositories with rate limits. Product plans are external state, so confirm the terms again before relying on them.

Private repositories remain Copilot-only unless a candidate's approved plan, data terms, and GitHub App permissions receive a separate review.

<!-- Docs -->

[pr-reviewer-evaluation]: ./pr-reviewer-evaluation.md

<!-- GitHub -->

[pr-892]: https://github.com/ptr727/ProjectTemplate/pull/892

<!-- External -->

[coderabbit-auto-review]: https://docs.coderabbit.ai/configuration/auto-review
[coderabbit-config]: https://docs.coderabbit.ai/reference/configuration
[coderabbit-plans]: https://docs.coderabbit.ai/management/plans
[copilot-customize]: https://docs.github.com/en/copilot/tutorials/customize-code-review
[copilot-exclusion]: https://docs.github.com/en/copilot/how-tos/configure-content-exclusion/exclude-content-from-copilot
[qodo-config]: https://docs.qodo.ai/install-and-configure/configuration-overview/configuration-file
[qodo-ignore]: https://docs.pr-agent.ai/usage-guide/additional_configurations/
[qodo-rules]: https://docs.qodo.ai/governance/rule-enforcement/building-review-standards
[qodo-verbosity]: https://docs.qodo.ai/code-review/review-verbosity
