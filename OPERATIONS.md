# Operations

How this repository is run. It ships no application code, so its operations are the fleet audit, the local gates that mirror CI, and the script that applies repository configuration.

## Runbooks

### Run the gates the way CI runs them

CI passes explicit `--check` lists, and a bare `prose_lint.py <file>` runs `DEFAULT_RULES`, which omits `comment-wrap`, `comment-case` and `sentence-split`. A bare run therefore under-reports and a clean result from it proves less than it appears to. Run the CI invocations:

```sh
python3 scripts/test_prose_lint.py
python3 scripts/test_repo_gate.py
python3 scripts/test_pr_review.py
python3 spec/audit.py --selftest
python3 scripts/repo_gate.py
python3 scripts/prose_lint.py . --check charset --check dupword --check spelling
python3 scripts/prose_lint.py . --check charset-unknown --check semicolon --check dash --check comment-wrap --check comment-case --summary
python3 spec/validate.py
docker run --rm --pull=always -v "$PWD":/check --workdir /check mstruebing/editorconfig-checker:latest
```

Run the `editorconfig-checker` line before pushing any new file. This repository defaults to CRLF, most tooling writes LF, and a new file therefore fails that check on its first CI run rather than locally.

The first prose invocation gates. The second is warn-only and reports the backlog that is corrected as each file is next edited.

Scope a run to what changed, which matches the correct-as-next-edited rule:

```sh
python3 scripts/prose_lint.py . --diff origin/develop
```

`prose_lint.py` reads only files git tracks, so a new file reports clean until it is staged. The first clean run on an unstaged file is vacuous.

### Audit the fleet

```sh
python3 spec/audit.py                 # every cataloged repo
python3 spec/audit.py <RepoName>      # one repo
python3 spec/audit.py --issue <RepoName>
```

Findings are a point-in-time snapshot read live over the API. Re-run before acting on one, and quote the run stamp in any issue derived from it. The deterministic subset lives here, and the full letter-and-intent verdict is [AUDIT.md](./AUDIT.md).

### Apply or verify repository configuration

```sh
repo-config/configure.sh check <owner>/<repo> <release|operational>
repo-config/configure.sh apply <owner>/<repo> <release|operational>
```

`check` is read-only and exits non-zero on drift. `apply` is idempotent and drives entirely from the committed payloads, so it is a no-op on a conformant repo.

`apply` is not a narrow toggle. One run patches every key in `repo-config/settings.json`, sets the default branch, enables both Dependabot features, and creates or updates both branch rulesets. On a repository that has deliberately drifted it silently reasserts the fleet configuration.

The model argument selects which develop payload is applied, so passing the wrong one applies the wrong ruleset.

## Backup and Recovery

The repository is the record, and GitHub holds it. Nothing here keeps state outside git.

A deleted branch is recoverable from any full clone that still has the commit, which is the recovery path when a branch is deleted while another pull request is based on it:

```sh
git push origin <sha>:refs/heads/<branch>
```

Never use `--depth 1` on a clone that will amend or force-push, because a shallow clone severs the merge base and orphans the branch.

## Logs and Debugging

Workflow runs are the log. `gh run list --branch <branch>` and `gh run view <id> --log-failed` reach them.

A local gate reproduces a CI failure exactly, because CI runs the same commands listed under Runbooks against the same committed configuration. Reproduce locally before reading workflow logs.

## Tool Usage

The Docker linters pull `:latest` deliberately, so a local run matches whatever CI resolved:

```sh
docker run --rm --pull=always -v "$PWD:/workdir" davidanson/markdownlint-cli2:latest "**/*.md"
docker run --rm --pull=always -v "$PWD:/workdir" ghcr.io/streetsidesoftware/cspell:latest --no-progress "**/*.md"
```

The `editorconfig-checker` action is setup-only. Using it alone silently skips the check, so CI invokes the checker itself rather than relying on the action.

Two `gh` limitations on the current host, both worked around rather than fixed:

- `gh pr checks --json` does not exist before `gh` 2.50, so a watcher built on it prints nothing and a quiet result reads as a passing one.
- `gh pr edit --base` fails with a Projects-classic deprecation error. Use `gh api --method PATCH repos/<owner>/<repo>/pulls/<number> -f base=<branch>` instead.

## Configuration Layout

- [spec/](./spec/) is the machine-readable ground truth, holding project types, the file and section baseline, and required or forbidden secrets.
- [registry/repos.json](./registry/repos.json) is the fleet registry, naming every project with its types, publish mechanism, and status.
- [repo-config/](./repo-config/) holds the branch rulesets and the apply script. It sits outside `.github/`, which is Actions-owned.
- [catalog/](./catalog/) holds reference snippets the audit compares implementations against.
- [reports/](./reports/) holds per-repo audit output.
- [scripts/](./scripts/) holds the gates that run in CI and locally.
