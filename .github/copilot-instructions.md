# Copilot Instructions

Repository-wide instructions for GitHub Copilot.

Read [AGENTS.md](../AGENTS.md) first. It routes every standing repository rule to its canonical
document. When performing code review, load and follow the `code-review` skill in
`.github/skills/code-review/SKILL.md`, then load every language, documentation, or workflow skill
that it selects for the changed files. GitHub Copilot reads these files from the pull request's
head branch, so review the instructions in that tree.

Do not duplicate rules from `AGENTS.md`, `GOVERNANCE.md`, `CODESTYLE.md`, or `WORKFLOW.md` here.
This file contains only Copilot-specific bootstrap and output requirements.

## Commit Messages and Pull Request Titles

Use an imperative subject of at most 72 characters with no trailing period. Use US English and
title case with lowercase short bind words. Do not add `Co-Authored-By:` unless requested. Do not
put a release-bump magnitude in the title. The full contract is in
[GOVERNANCE.md "Pull Request Title and Commit Message Conventions"](../GOVERNANCE.md#pull-request-title-and-commit-message-conventions).

## Reviewing Carried Fleet Content

Follow the fidelity declared for the file. A byte-locked reference to shared infrastructure that
this repository does not carry is intentional, not a broken link. Raise substantive defects in
canonical content, but locate the fix at its canonical source instead of proposing a local edit
that its fidelity rejects.

## GitHub Copilot Review Runbook

For every review:

1. Read the full pull request diff and count its changed files.
2. Follow `.github/skills/code-review/SKILL.md` and every skill it selects.
3. Publish every supported finding. Never suppress a finding or place it in a low-confidence or
   hidden findings block.
4. Use an inline comment when a changed line can anchor the finding. Use the review body only when
   no valid inline anchor exists.
5. End the review body with the exact machine-readable marker required by the `code-review` skill.

The review automation is `scripts/pr_review.py`, run from a hub checkout. Use its `status`, `wait`,
`comment`, and `reply --resolve` commands instead of reconstructing GraphQL queries or copying
review identifiers by hand. Use `comment` for a suppressed-finding answer in the pull request
conversation. Its status gate verifies the current head, diff coverage, output shape, inline
threads, body-only findings, and required checks.

A formal review with no findings is complete only when it covers the current head and states full
diff coverage. A refusal, partial or absent coverage statement, unrecognized output shape,
unresolved thread, or body-only finding blocks the review loop. Re-run the loop after every fix
push. Never infer review completion from `mergeStateStatus: CLEAN`.

Review effort is user-controlled. The automation observes `Lite`, `Balanced`, or `Max`, including an inherited `Default (<level>)`, and never selects or changes the setting. Effort does not determine coverage or completion. A request can complete without a `copilot_work_started` event, so absence of that event is not a stalled-review verdict. When `wait` returns `PENDING` with `requested=yes`, report the state and rerun `wait` for another bounded interval by default. Do not clear the request automatically because it may be active. If the maintainer directs a retry, remove Copilot in the pull request UI, add it again, and rerun `wait`. This recovery replaces only the review request and never changes the effort setting.

### Disproved Claims

**A disproof is proof about this repository, and the thread it was written in is not where the next round looks.** [GOVERNANCE.md "PR Review Etiquette"](../GOVERNANCE.md#pr-review-etiquette), which routes to the `pr-review-conduct` Skill, closes a false finding by disproving it in the thread, addressed to the reviewer so it does not raise the same thing again, and while the pull request is open that is the right place for it. Afterwards it is the wrong one. The pull request merges, the next round begins with no memory of the last, and the second occurrence reaches a maintainer with no way to tell it from a first. Each entry below is a claim that was tested against this repository and found false, kept so the proof is read rather than built twice.

**An entry names the claim, what was run or read to disprove it, the revision it was proved against, and what ends it.** A disproof is true of one tree at one revision, so an entry whose subject moves is deleted by the change that moves it rather than edited to look current, which is the same sweep the [GOVERNANCE.md "Documentation Style Conventions"](../GOVERNANCE.md#documentation-style-conventions) rule already requires of prose asserting a behavior that has changed underneath it. This is deliberately not a list to append to, since an entry outliving the code it was proved against becomes a reason not to check, and that is strictly worse than proving the claim a second time.

**The record answers a repeated claim and never dismisses a new one.** An entry is cited only where the revision it names is still what the tree carries, and the reply carries the proof re-read rather than a pointer to the entry, since a reviewer that cannot open this file learns nothing from being pointed at it. Judge a finding on its merits first and match it against this record second, because reading it the other way round is how a real finding gets closed by a stale proof.

**The entries are this repository's own.** Each names a file and a revision, so a repository holding a copy of this file carries the shape and the rules above rather than these findings, deletes an entry whose subject it does not carry, and records what it has proved itself.

- **`keys_unsorted` requires jq 1.6, so the ruleset normalizer in `repo-config/configure.sh` fails to compile on jq 1.5.** Raised as a suppressed finding, by analogy to the `walk/1` call the same filter was rewritten to avoid.
  - **Disproved by** - running both builtins on `jq-1.5-1-a5b5cbe`, the build that reproduces the `walk/1` failure. `keys_unsorted` evaluates there and the whole normalizer returns the sorted document, while `walk(.)` on that binary answers `jq: 1 compile error`. The two builtins are not in the same position, and the analogy is the whole of what carried the finding.
  - **Proved against** - the `norm` filter in `repo-config/configure.sh` on `develop` at `756a53e`.
  - **Delete when** - the filter stops calling `keys_unsorted`, or nothing this check runs on carries a jq older than 1.6.

- **Splitting the fallback parse in `host-setup/agent-safety/gh-write-guard.py` a line at a time mis-reads a newline inside a quoted argument, reintroducing the false deny that path exists to remove.** Raised against the branch that made a newline end a command, on the ground that a `--body` argument holding a newline and a `git push origin develop` would have that line read as a push.
  - **Disproved by** - the arm being unreachable, and then by measuring it rather than resting on that. `punctuation_chars` arrived in Python 3.6, the module uses f-strings throughout, and `install.py` refuses to install below 3.7, so an interpreter that would raise the `TypeError` fails to import the module before reaching the fallback. Simulated against a `shlex` that rejects the keyword and passes everything else through, the quoted-newline example is allowed on both paths, because splitting a line whose quoting cannot be parsed leaves the quote glued to the token and the push target reads as `develop"`, matching no branch. The shape does bite one line further out, where a three-line body whose middle line is a bare `git push origin develop` denies on the forced path, and the alternative is worse where it counts: parsing the whole command at once keeps a quoted newline intact and drops every real one, so an ordinary push followed by a `gh pr create` denies under every interpreter rather than under none.
  - **Proved against** - `_git_subcommand_arglists` in `host-setup/agent-safety/gh-write-guard.py` and the interpreter floor in `host-setup/agent-safety/install.py`, on `develop` at `dbd1cdc`.
  - **Delete when** - the floor drops below 3.6, or the fallback stops splitting the command a line at a time.
  - **Earned anyway** - a test case rather than a change. Only `ValueError` from unbalanced quoting reaches that path in practice and nothing covered it, so a finding wrong about its own reachability was right that the path was untested.

- **A description's stale commit claims are found by extracting the bare SHAs it quotes.** Not a reviewer's finding but the method this repository's own backlog specified for the `claims` check in `scripts/pr_review.py`, recorded here because a rejected method costs the same to re-propose as a declined finding costs to re-derive, and because a backlog has a place for a claim the tree contradicts and none for a method a measurement rejects.
  - **Disproved by** - running it over the 25 most recent merged pull requests, where it raised four references and all four were correct prose: a `develop` commit named as history, a SHA inside a pasted digest, and two commits in another repository written without a URL. Nothing in the shape of a bare SHA separates those from a claim, and separating them by meaning is the similarity heuristic [GOVERNANCE.md "Documentation Style Conventions"](../GOVERNANCE.md#documentation-style-conventions) rules out. A path arm measured on the same corpus is worse, flagging 54 of 215 backticked candidates, nearly all of them bare basenames and other repositories.
  - **Proved against** - the 25 most recent merged pull requests as of `develop` at `756a53e`, the corpus on which the anchored verb form that ships instead raises one reference, and that one true.
  - **Delete when** - `claims` stops reading a description for commit references.

- **The GraphQL `pullRequests` connection defaults to `states: [OPEN]`, so the bot node id query in "Triggering and Polling" returns nothing in a repository whose Copilot-reviewed pull requests have all merged.** Raised against the repo-wide read, on the ground that a cold start is exactly the case where no open pull request carries a review.
  - **Disproved by** - running the connection both ways against this repository while exactly one pull request was open. With `states` omitted, `pullRequests(first: 5, orderBy: { field: CREATED_AT, direction: DESC })` answers `628 OPEN`, `627 MERGED`, `626 MERGED`, `625 MERGED` and `624 MERGED`, so the omitted default is every state rather than `OPEN`. The same call with `states: [OPEN]` answers `628 OPEN` alone, which is the behavior the finding predicts for the first form and is what distinguishes them. The read was first run when this repository had no open pull request at all, and it returned the id from merged ones.
  - **Proved against** - the `BOT_ID` query in "Triggering and Polling" in this file, run against this repository's pull request list on 2026-08-08.
  - **Delete when** - that query names `states` explicitly, or stops reading pull requests to find the id.

- **"The agent check branches" in `STANDUP.md` section 0 is a subject-verb disagreement, and should read "The agent checks branches".** Raised as a suppressed finding against a line the change under review only touched as diff context.
  - **Disproved by** - reading the sentence against the snippet it describes. The subject is the noun phrase "the agent check", meaning the check for the signing agent, and "branches" is its verb, which is what the `if [ ... = ssh ]; then ssh-add -L; else gpg --list-secret-keys; fi` line does. The proposed reading needs "branches" as a plural noun, and the paragraph is section 0, before a repository exists, where the alternatives it names are the SSH and GPG forms rather than refs.
  - **Proved against** - the paragraph following the agent snippet in `STANDUP.md` section 0 on `develop` at `676a2bd`, unchanged since `77be3a3`.
  - **Delete when** - the sentence is reworded for any reason, since the entry is about this phrasing rather than about the rule it states.

- **A hyphenated key such as `inputs.app-login` or `inputs.delete-branch` cannot be read with dot notation in a GitHub Actions expression, since `-` parses as subtraction, so the merge-bot task's `if:` conditions never match and its `env:` values never resolve.** Raised as four suppressed findings against `.github/workflows/merge-bot-task.yml`, each a variant of the one claim.
  - **Disproved by** - reading the expressions reference, which states that property dereference syntax needs a name that starts with a letter or `_` and contains only alphanumeric characters, `-`, or `_`, so a hyphen is inside the allowed set and index syntax is required only for a name outside it. And by the same file, whose every job reads `steps.app-token.outputs.token` with dot notation on the same hyphenated shape, the expression the fleet's merge-bot has resolved on every Dependabot merge it has performed. `actionlint` parses the file without a finding.
  - **Proved against** - `.github/workflows/merge-bot-task.yml` on `feature/reusable-workflows` at `210d88f`, and the "About contexts" property dereference rule in the GitHub Actions contexts reference read on 2026-08-15.
  - **Delete when** - the task stops declaring hyphenated inputs, or the expressions reference changes the allowed set.

- **`sudo_implementations()` in `host-setup/linux/install-tools.sh` never matches, because `update-alternatives --query sudo` prints a slave as `visudo:` with a trailing colon, so a host whose active `visudo` rejects the drop-in always reaches the "No sudo on this host parses timestamp_type" refusal even where an alternative would parse it.** Raised as a suppressed finding against the sudo timestamp action.
  - **Disproved by** - running the subcommand the function actually calls. `update-alternatives --query sudo` prints each slave as two space-separated fields under `Slaves:`, one space-indented line reading `visudo /usr/sbin/visudo.ws`, and the awk over that output prints `/usr/bin/sudo.ws /usr/sbin/visudo.ws` and `/usr/lib/cargo/bin/sudo /usr/lib/cargo/bin/visudo`. The colon form the finding describes belongs to `update-alternatives --display sudo`, a different subcommand, which prints `slave visudo: /usr/sbin/visudo.ws`. The path the finding says is unreachable was also driven end to end in an `ubuntu:25.10` container, where the run found `/usr/bin/sudo.ws`, switched the alternative to it, and wrote the drop-in.
  - **Proved against** - `sudo_implementations` in `host-setup/linux/install-tools.sh` on `feature/sudo-timestamp-global` at `13e689f`, against `update-alternatives` 1.22.x on Ubuntu 25.10.
  - **Delete when** - the function stops reading `update-alternatives --query`, or that subcommand changes its slave format.

## When in Doubt

Stop and report the uncertainty. Do not guess at an instruction, suppress a possible finding, or
claim coverage that the review did not perform.
