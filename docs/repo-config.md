# Repository Configuration (Hub-Only)

The process for applying, checking, and regenerating the canonical repository configuration. This document and the entire `repo-config/` directory are hub-only. Every command runs from a hub checkout at `main` and names its target repository.

## Configuration Source

The hub holds all fleet-wide repository configuration:

- `settings.json` declares the shared repository settings.
- `main.json` declares the shared `main` ruleset.
- `develop.json` declares the release-model `develop` ruleset.
- `operational/develop.json` declares the operational-model `develop` ruleset.
- `configure.sh` applies or checks those payloads through the GitHub API.

Downstream repositories carry no `repo-config/` directory. The registry's `workflowModel` selects the `develop` payload. Commands that operate before registry enrollment pass the model explicitly.

The carried `AUDIT.md` reaches the hub at `main` for its configuration check. The fleet-wide `spec/audit.py` reads the same hub payloads directly. Both paths compare live state against one source.

## Per-Repository Secrets

Downstream repositories carry no copy of `spec/secrets.json`. `baseline` applies to every fleet repo by definition, and `mechanisms`/`targetMechanisms`/`typeMechanisms` resolve from the registry entry (a repo's `publish[]` targets and `types[]`) rather than from anything repo-specific, so a per-repo copy could only restate the hub's own computation or drift from it between resyncs. `spec/audit.py <RepoName>`, run from a hub checkout, resolves the required set the same way the mechanized `repo-setup` check does: `targetMechanisms`/`typeMechanisms` selects each applicable mechanism from the registry entry, `baseline` plus the selected `mechanisms` gives the required and forbidden names, and the result is cross-checked against the repo's live Actions and Dependabot secret stores.

## Applying the Config

**Configure by importing the JSON payloads, never by hand-building the rules** (hand reconstruction has gone wrong on past setups). The result must be **exactly two rulesets named `develop` and `main`**, and the names are load-bearing (`AGENTS.md` and the workflows reference them). Only the `develop` *content* varies by model.

Remove all classic branch-protection rules and stray rulesets. Run `configure.sh apply` from a hub checkout at `main`, naming the target repository and its model. The script applies `settings.json`, the Dependabot security features, and both rulesets. A registered repository can omit the model and use the registry lookup. A repository outside the registry passes the model explicitly:

```sh
repo-config/configure.sh apply owner/repo release|operational
```

Then validate the result with `repo-config/configure.sh check owner/repo release|operational`, run from the same checkout, which asserts every applied ruleset, setting, and security feature and exits non-zero on drift (the ruleset and settings checks are driven by the committed payloads, so they stay repo-agnostic). Or import each ruleset by hand with `gh api -X POST repos/<owner>/<repo>/rulesets --input repo-config/<name>.json` (operational repos use `operational/develop.json` for `develop`). `gh ruleset` is read-only, so creation goes through `gh api`. The required check binds by name and only turns green after the repo's PR workflow runs once. To edit a live ruleset, GET it, change the field, and PUT the whole writable subset back (a partial PUT `422`s).

## Regenerating the Payloads

To change the canonical rulesets, edit the live rulesets (fleet-wide changes happen at the hub), then regenerate the committed files from the current repo:

```sh
model=release # Set to release or operational for the payload set being regenerated.
repo="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
# Paginate so a name match on a later page is never missed - the same trap configure.sh guards against.
# --paginate with --jq '.[]' emits one JSON object per ruleset across all pages; jq -s re-assembles them
# into the single array the selections below expect.
rulesets=$(gh api --paginate "repos/$repo/rulesets" --jq '.[]' | jq -s '.')
for name in develop main; do
  out="repo-config/$name.json"
  # The operational model writes its develop payload under operational/; both files exist in the hub.
  [ "$name" = "develop" ] && [ "$model" = "operational" ] && out="repo-config/operational/develop.json"
  # Exactly one ruleset per name: zero or duplicates is declared drift - fail loudly, never regen from a guess.
  count=$(jq --arg n "$name" '[.[] | select(.name==$n)] | length' <<<"$rulesets")
  [ "$count" -eq 1 ] || { echo "expected exactly 1 ruleset named $name, found $count (drift)" >&2; exit 1; }
  id=$(jq --arg n "$name" '.[] | select(.name==$n) | .id' <<<"$rulesets")
  # bypass_actors is left out, since a payload that declared one would assert this repo's bypass list against every repo diffed on it.
  gh api "repos/$repo/rulesets/$id" \
    --jq '{name, target, enforcement, conditions, rules}' \
    | jq -S --indent 4 '.' > "$out"
done
```

## Brownfield Migration (Maintainer Only)

`Require signed commits` rejects any pre-existing unsigned commit, so the first `develop -> main` release on a repo with unsigned history is blocked. Re-signing that history is a non-fast-forward that the `Block force pushes` rule rejects, **and the admin bypass does not cover `git push --force`**. Completing it requires temporarily disabling the ruleset and a maintainer force-push. This is a one-time, maintainer-performed migration that deliberately uses the force-push [GOVERNANCE.md "Git and Commit Rules"][governance-git-and-commit-rules] forbids agents from running. **An agent must never execute it, and must surface it to the maintainer instead.** Greenfield repos where signing is live before the first commit never hit this. When the rewrite touches commits committed under a bot or web-flow identity (`dependabot[bot]`, `github-actions[bot]`), set each commit's committer to the signing identity before re-signing so the committer GitHub verifies matches your key, rather than leaving your key over another identity's commit (see the history-rewrite rule in [GOVERNANCE.md "Git and Commit Rules"][governance-git-and-commit-rules]).

<!-- Repo -->

[governance-git-and-commit-rules]: ../GOVERNANCE.md#git-and-commit-rules
