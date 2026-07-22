# repo-config: Carry, Apply, and Regenerate (Hub-Only)

The **process** for carrying the `repo-config/` baseline to a fleet repo, applying it, and regenerating the canonical payloads. This doc is **hub-only** - it is not carried downstream (it describes what the hub does *to* a repo, not a fact about any one repo). The carried [`repo-config/README.md`][repo-config-readme] states only the current facts about a repo's own config. This carry/apply/regen procedure lives here so it never ships into a downstream copy.

## Downstream Carry

Every fleet repo carries the `repo-config/` directory. The hub keeps the canonical copy. Rules for the carried copy:

- **Carry only your model's `develop` variant.** A `release` repo carries `develop.json`. An `operational` repo carries `operational/develop.json` instead. `main.json` and `settings.json` are shared by both models. `configure.sh` aborts when the payload its model needs is missing rather than applying a partial configuration.
- **Carried files reference no other fleet repo.** A carried file names no sibling fleet repo as an example and links none (any fleet repo may be private, so a cross-repo link 404s in a public carrier, and it couples the repos). See [AGENTS.md "Documentation Style Conventions"][agents-documentation-style]. To point at a current good example, name it in the onboarding/conformance issue or the hub-only [`reports/conformance-matrix.md`][conformance-matrix].
- **Adapted self-audit carry.** A downstream repo carries **locally adapted** `AUDIT.md` and `spec/secrets.json`, scoped to self-auditing its own rulesets, settings, and secrets against the committed `repo-config/` baseline - the standard shape, so the carried tooling is self-contained. The hub's fleet-wide audit remains authoritative. The adapted `AUDIT.md` is a settings diff, a normalized ruleset diff against the carried payloads (an operational carry swaps in `operational/develop.json`), and a names-only secrets check, all targeting the current repo - adapt this shape, don't invent. A current well-formed example is named in the onboarding/conformance issue.
- **Adapted `spec/secrets.json` shape.** The repo-scoped adaptation carries `baseline` (the App pair, which every fleet repo needs for the merge-bot) plus a `mechanisms` entry for each publish mechanism the repo actually uses, and the `targetMechanisms` routing entries for those mechanisms. **A source-only repo whose publish targets all map to a null mechanism (nothing to route) carries just `baseline` (plus a `note`)** - it omits `targetMechanisms` and `mechanisms` entirely, because a lone `targetMechanisms` map with no `mechanisms` reads as a schema bug (the audit enumerates `baseline` + `mechanisms`, never `targetMechanisms`, so an all-null routing map is dead weight). A `release` repo that uses a real mechanism (e.g. `nuget-oidc`, `docker-hub`, `codecov`) carries that `mechanisms` entry **and** its `targetMechanisms`/`typeMechanisms` routing, which the audit then picks up.
- **The regen snippet targets the current repo**, so it works unchanged in a carried copy.

## Applying the Config

**Configure by importing the JSON payloads, never by hand-building the rules** (hand reconstruction has gone wrong on past setups). The result must be **exactly two rulesets named `develop` and `main`** - the names are load-bearing (`AGENTS.md` and the workflows reference them). Only the `develop` *content* varies by model.

First remove all legacy classic branch-protection rules and any stray rulesets, then run `configure.sh` (which picks the `develop` payload from the repo's `workflowModel` and applies `settings.json` alongside the rulesets):

```sh
repo-config/configure.sh [owner/repo] [release|operational]
```

Or import each ruleset by hand with `gh api -X POST repos/<owner>/<repo>/rulesets --input repo-config/<name>.json` (operational repos use `operational/develop.json` for `develop`). `gh ruleset` is read-only, so creation goes through `gh api`. The required check binds by name and only turns green after the repo's PR workflow runs once. To edit a live ruleset, GET it, change the field, and PUT the whole writable subset back (a partial PUT `422`s).

## Regenerating the Payloads

To change the canonical rulesets, edit the live rulesets (fleet-wide changes happen at the hub), then regenerate the committed files from the current repo:

```sh
repo="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
# Paginate so a name match on a later page is never missed - the same trap configure.sh guards against.
# --paginate with --jq '.[]' emits one JSON object per ruleset across all pages; jq -s re-assembles them
# into the single array the selections below expect.
rulesets=$(gh api --paginate "repos/$repo/rulesets" --jq '.[]' | jq -s '.')
for name in develop main; do
  out="repo-config/$name.json"
  # An operational carry keeps its develop payload at operational/develop.json (develop.json is absent).
  [ "$name" = "develop" ] && [ ! -f "$out" ] && out="repo-config/operational/develop.json"
  # Exactly one ruleset per name: zero or duplicates is declared drift - fail loudly, never regen from a guess.
  count=$(jq --arg n "$name" '[.[] | select(.name==$n)] | length' <<<"$rulesets")
  [ "$count" -eq 1 ] || { echo "expected exactly 1 ruleset named $name, found $count (drift)" >&2; exit 1; }
  id=$(jq --arg n "$name" '.[] | select(.name==$n) | .id' <<<"$rulesets")
  gh api "repos/$repo/rulesets/$id" \
    --jq '{name, target, enforcement, bypass_actors, conditions, rules}' \
    | jq -S --indent 4 '.' > "$out"
done
```

## Brownfield Migration (Maintainer Only)

`Require signed commits` rejects any pre-existing unsigned commit, so the first `develop -> main` release on a repo with unsigned history is blocked. Re-signing that history is a non-fast-forward that the `Block force pushes` rule rejects, **and the admin bypass does not cover `git push --force`**. Completing it requires temporarily disabling the ruleset and a maintainer force-push. This is a one-time, maintainer-performed migration that deliberately uses the force-push [AGENTS.md "Git and Commit Rules"][agents-git-and-commit-rules] forbids agents from running - **an agent must never execute it - surface it to the maintainer**. Greenfield repos where signing is live before the first commit never hit this.

<!-- Repo -->

[agents-documentation-style]: ../AGENTS.md#documentation-style-conventions
[agents-git-and-commit-rules]: ../AGENTS.md#git-and-commit-rules
[conformance-matrix]: ../reports/conformance-matrix.md
[repo-config-readme]: ../repo-config/README.md
