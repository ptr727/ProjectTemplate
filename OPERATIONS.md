# Operations

How this repository is run. It ships no application code, so its operations are the fleet audit, the gates that mirror CI, and the script that applies repository configuration. Those gates and that script serve the whole fleet from this checkout rather than being carried into each repository, per [GOVERNANCE.md "Hub-Hosted Tooling"](./GOVERNANCE.md#hub-hosted-tooling), so every run below is a run from here against a repository named on the command line.

## Runbooks

### Run the gates the way CI runs them

CI passes explicit `--check` lists, and a bare `python3 scripts/prose_lint.py [file]` runs `DEFAULT_RULES`, which is those two lists together. What differs is the exit code rather than the coverage: CI gates on `charset`, `dupword` and `spelling` and reports the other five warn-only, where a bare run exits non-zero on any of the eight. `sentence-split` is in neither and is asked for by name. Run the CI invocations:

```sh
python3 scripts/test_prose_lint.py
python3 scripts/test_repo_gate.py
python3 scripts/test_pr_review.py
python3 spec/audit.py --selftest
python3 scripts/repo_gate.py
python3 scripts/prose_lint.py . --check charset --check dupword --check spelling
python3 scripts/prose_lint.py . --check charset-unknown --check semicolon --check dash --check comment-wrap --check comment-case --summary
for f in registry/*.json spec/*.json repo-config/*.json; do jq empty "$f"; done
python3 spec/validate.py
docker run --rm --pull=always -v "$PWD":/check --workdir /check mstruebing/editorconfig-checker:latest
```

Two gaps in that list are CI's rather than this runbook's, reproduced here so a local run matches CI rather than quietly exceeding it. The `jq` glob covers `repo-config/*.json` and does not reach `repo-config/operational/develop.json`, so a malformed operational payload passes. And `sentence-split` is implemented and tested but named by no invocation, so nothing runs it.

Run the `editorconfig-checker` line before pushing any new file. This repository defaults to CRLF, most tooling writes LF, and a new file therefore fails that check on its first CI run rather than locally.

The first prose invocation gates. The second reports the backlog that is corrected as each file is next edited, and it exits non-zero locally whenever findings exist. It is warn-only in CI because the workflow step sets `continue-on-error: true`, not because the command is lenient, so a non-zero exit locally is the expected result rather than a problem.

Scope a run to what changed, which matches the correct-as-next-edited rule:

```sh
python3 scripts/prose_lint.py . --diff origin/develop
```

Whole-tree discovery reads only files git tracks, so `python3 scripts/prose_lint.py .` and `--diff` do not see a new file until it is staged, and a clean whole-tree run proves nothing about an unstaged one. An explicit path is always read, tracked or not, so name a new file directly to check it before staging.

### Audit the fleet

```sh
python3 spec/audit.py                 # every cataloged repo
python3 spec/audit.py [RepoName]      # one repo
python3 spec/audit.py --issue [RepoName]
```

Findings are a point-in-time snapshot read live over the API. Re-run before acting on one, and quote the run stamp in any issue derived from it. The deterministic subset lives here, and the full letter-and-intent verdict is [AUDIT.md](./AUDIT.md). No project-type check in `spec/project-types.json` runs here, and the cross-cutting ones are covered only in part, so read a clean run as evidence for the subset above and not for AUDIT.md section 4.

### Apply or verify repository configuration

```sh
repo-config/configure.sh check [owner/repo] [release|operational]
repo-config/configure.sh apply [owner/repo] [release|operational]
```

**Always pass the command, and always name the repository.** A bare `repo-config/configure.sh` with no arguments defaults to `apply` against the current repo, so an invocation meant to test whether the script runs performs a live write instead. Never run it without a command. The repository argument matters for the same reason now that the fleet runs this copy rather than its own: an omitted target resolves to this repository, and applying the fleet configuration to the hub while meaning to configure a downstream repo is a well-formed write to the wrong place.

`check` is read-only and exits non-zero on drift. `apply` is idempotent and drives entirely from the committed payloads, so it is a no-op on a conformant repo.

`apply` is not a narrow toggle. One run patches every key in `repo-config/settings.json`, sets the default branch, enables both Dependabot features, and creates or updates both branch rulesets. On a repository that has deliberately drifted it silently reasserts the fleet configuration.

The model argument selects which develop payload is applied, so passing the wrong one applies the wrong ruleset.

## Backup and Recovery

The repository is the record, and GitHub holds it. Nothing here keeps state outside git.

A deleted branch is recoverable from any full clone that still has the commit, which is the recovery path when a branch is deleted while another pull request is based on it:

```sh
git push origin [sha]:refs/heads/[branch]
```

Never use `--depth 1` on a clone that will amend or force-push, because a shallow clone severs the merge base and orphans the branch.

## Logs and Debugging

Workflow runs are the log. `gh run list --branch [branch]` and `gh run view [id] --log-failed` reach them.

A local gate reproduces a CI failure exactly, because CI runs the same commands listed under Runbooks against the same committed configuration. Reproduce locally before reading workflow logs.

## Tool Usage

The Docker linters pull `:latest` deliberately, so a local run matches whatever CI resolved:

```sh
docker run --rm --pull=always -v "$PWD":/workdir --workdir /workdir davidanson/markdownlint-cli2:latest "**/*.md"
docker run --rm --pull=always -v "$PWD":/workdir --workdir /workdir ghcr.io/streetsidesoftware/cspell:latest --no-progress README.md HISTORY.md
```

Both commands are the canonical invocations from [GOVERNANCE.md](./GOVERNANCE.md). markdownlint reads every Markdown file, while cspell reads `README.md` and `HISTORY.md` only. That narrower spelling scope is deliberate, since gating every Markdown file would mean padding `cspell.json` with technical terms without end, and broad live spell-check is the editor extension's job. Widening it here produces noise that no gate acts on.

The `editorconfig-checker` action is setup-only. Using it alone silently skips the check, so CI invokes the checker itself rather than relying on the action.

Two `gh` limitations on the current host, both worked around rather than fixed:

- `gh pr checks` carries no `--json` flag on the installed `gh` 2.46.0, so a watcher built on it prints nothing and a quiet result reads as a passing one. Read the checks from `gh pr view --json statusCheckRollup` instead.
- `gh pr edit --base` fails with a Projects-classic deprecation error. Use `gh api --method PATCH repos/[owner/repo]/pulls/[number] -f base=[branch]` instead.

## Configuration Layout

- [spec/](./spec/) is the machine-readable ground truth, holding project types, the file and section baseline, and required or forbidden secrets.
- [registry/repos.json](./registry/repos.json) is the fleet registry, naming every project with its types, publish mechanism, and status.
- [repo-config/](./repo-config/) holds the branch rulesets, the fleet settings, and the apply script. The payloads carry to the fleet and the script is reached here. It sits outside `.github/`, which is Actions-owned.
- [catalog/](./catalog/) holds reference snippets the audit compares implementations against.
- [reports/](./reports/) holds per-repo audit output.
- [scripts/](./scripts/) holds the gates that run in CI and locally, and that every fleet repository reaches rather than carries.
