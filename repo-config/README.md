# repo-config

Repository and branch configuration held as committed files, kept out of `.github/` (which is reserved for GitHub-Actions-owned content). This mirrors the layout the fleet repos use.

- `main.json`, `develop.json` - the branch rulesets as the writable API subset (`name`, `target`, `enforcement`, `bypass_actors`, `conditions`, `rules`). These are the canonical expected payload the audit ([AUDIT.md][audit]) diffs each repo's live rulesets against.
- `configure.sh` - applies the rulesets to a repository via the GitHub API (create or full-payload update, idempotent). Run `repo-config/configure.sh [owner/repo]`.

## Rulesets

`main` requires merge-commit merges (no linear-history rule); `develop` requires squash merges with linear history. Both require signed commits, a passing `Check pull request workflow status`, resolved review threads, and Copilot review, and block force-pushes and deletion. Both intentionally leave "Require branches to be up to date before merging" **off** - see [AGENTS.md "Branching Model"][agents-branching-model].

**Configure by importing these JSON files, never by hand-building the rules** (hand reconstruction has gone wrong on past setups). The result must be **exactly two rulesets named `develop` and `main`** - the names are load-bearing (`AGENTS.md` and the workflows reference them). First remove all legacy classic branch-protection rules and any stray rulesets, then run `configure.sh` (or `gh api -X POST repos/<owner>/<repo>/rulesets --input repo-config/<name>.json` per file). `gh ruleset` is read-only; creation goes through `gh api`. The required check binds by name and only turns green after `test-pull-request.yml` runs once. To edit a ruleset, GET it, change the field, and PUT the whole writable subset back (a partial PUT `422`s).

To change the canonical rulesets, edit the live rulesets here, then regenerate the committed files:

```sh
for name in develop main; do
  id=$(gh api repos/ptr727/ProjectTemplate/rulesets --jq ".[] | select(.name==\"$name\") | .id")
  gh api "repos/ptr727/ProjectTemplate/rulesets/$id" \
    --jq '{name, target, enforcement, bypass_actors, conditions, rules}' \
    | jq -S '.' > "repo-config/$name.json"
done
```

## Secrets

Publish credentials required per mechanism are enumerated in [spec/secrets.json][secrets]. NuGet and PyPI use keyless OIDC Trusted Publishing (no stored key; the publish job needs `id-token: write`, and PyPI additionally an `environment: pypi` gate). Docker Hub has no OIDC equivalent and uses a stored `DOCKER_HUB_USERNAME` + `DOCKER_HUB_ACCESS_TOKEN` in both the Actions and Dependabot secret stores. Codegen and merge-bot repos add a GitHub App (`CODEGEN_APP_CLIENT_ID` + `CODEGEN_APP_PRIVATE_KEY` in both stores; the app must be installed, not just created). App-token call sites use `client-id`, never the deprecated `app-id`.

## Repo Settings

- Default branch `main`. Enable both `Allow merge commits` and `Allow squash merging` at the repo level so each branch ruleset can pick its method; leave rebase disabled. Enable auto-merge.
- Actions / General: allow GitHub Actions to create and approve pull requests (for the bots).

## Brownfield Migration (Maintainer Only)

`Require signed commits` rejects any pre-existing unsigned commit, so the first `develop -> main` release on a repo with unsigned history is blocked. Re-signing that history is a non-fast-forward that the `Block force pushes` rule rejects, **and the admin bypass does not cover `git push --force`**. Completing it requires temporarily disabling the ruleset and a maintainer force-push. This is a one-time, maintainer-performed migration that deliberately uses the force-push [AGENTS.md "Git and Commit Rules"][agents-git-and-commit-rules] forbids agents from running - **an agent must never execute it; surface it to the maintainer**. Greenfield repos where signing is live before the first commit never hit this.

<!-- Repo -->

[agents-branching-model]: ../AGENTS.md#branching-model
[agents-git-and-commit-rules]: ../AGENTS.md#git-and-commit-rules
[audit]: ../AUDIT.md
[secrets]: ../spec/secrets.json
