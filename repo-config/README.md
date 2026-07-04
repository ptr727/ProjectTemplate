# repo-config

Repository and branch configuration held as committed files, kept out of `.github/` (which is reserved
for GitHub-Actions-owned content). This mirrors the layout downstream repos use.

- `main.json`, `develop.json` - the branch rulesets as the writable API subset
  (`name`, `target`, `enforcement`, `bypass_actors`, `conditions`, `rules`). These are the canonical
  expected payload the audit (`AUDIT.md`) diffs each repo's live rulesets against.
- `configure.sh` - applies the rulesets to a repository via the GitHub API (create or full-payload
  update, idempotent). Run `repo-config/configure.sh [owner/repo]`.

`main` requires merge-commit merges (no linear-history rule); `develop` requires squash merges with
linear history. Both require signed commits, a passing `Check pull request workflow status`, resolved
review threads, and Copilot review, and block force-pushes and deletion.
