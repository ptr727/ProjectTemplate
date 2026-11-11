# Repository Configuration (Hub-Only)

The process for applying, checking, and regenerating the canonical repository configuration. This document and the entire `repo-config/` directory are hub-only. Every command runs from a hub checkout at `main` and names its target repository.

## Configuration Source

The hub holds all fleet-wide repository configuration:

- `settings.json` declares the shared repository settings.
- `main.json` declares the shared `main` ruleset.
- `develop.json` declares the release-model `develop` ruleset.
- `operational/develop.json` declares the operational-model `develop` ruleset.
- `labels.json` declares the fleet label set.
- `project.json` declares the fleet project every repository is linked to, by owner, number, and title.
- `configure.sh` applies or checks those payloads through the GitHub API.
- `central/` holds the READMEs `scripts/central_config.py` writes into the central bot-settings repositories, per "Central Bot Settings" below.

Downstream repositories carry no `repo-config/` directory. The registry's `workflowModel` selects the `develop` payload, and its `environments` declares the deployment environments the check mode asserts. Both are read from `registry/repos.json` rather than from a payload here. A `check` before registry enrollment passes the model explicitly, and `apply` waits for the entry.

The carried `AUDIT.md` reaches the hub at `main` for its configuration check. The fleet-wide `spec/audit.py` reads the same hub payloads directly. Both paths compare live state against one source.

## Per-Repository Secrets

Downstream repositories carry no copy of `spec/secrets.json`. `baseline` applies to every fleet repo by definition. `mechanisms`/`targetMechanisms`/`typeMechanisms` resolve from the registry entry (a repo's `publish[]` targets and `types[]`) rather than from anything repo-specific. So a per-repo copy could only restate the hub's own computation or drift from it between resyncs. `uv run --no-project --script spec/audit.py [RepoName]`, run from a hub checkout, resolves the required set the same way the mechanized `repo-setup` check does. `targetMechanisms`/`typeMechanisms` selects each applicable mechanism from the registry entry. `baseline` plus the selected `mechanisms` plus the registry entry's own `requiredSecrets[]` (the repo's domain-specific additions) gives the required and forbidden names. The result is cross-checked against the repo's live Actions and Dependabot secret stores.

`configure.sh apply` sets those same names, resolved by the same `spec/secret_names.py`, from the host's `~/.secrets/` directory. `configure.sh check` asserts them per store. The file each value is read from follows the name's scope in `spec/secrets.json`. See [`repo-config/README.md` "Provisioning From `~/.secrets/`"][repo-config-readme-provisioning] for the file names, the strict-scope rule, and the `scaffold` command that pre-creates the files.

## Applying the Config

**Configure by importing the JSON payloads, never by hand-building the rules** (hand reconstruction has gone wrong on past setups). The result must be **exactly two rulesets named `develop` and `main`**, and the names are load-bearing (`AGENTS.md` and the workflows reference them). Only the `develop` *content* varies by model.

Remove all classic branch-protection rules and stray rulesets. Run `configure.sh apply` from a hub checkout at `main`, naming the target repository and its model. `apply` refuses, writing nothing, a repository with no entry in the registry it reads. Without the entry, the rulesets and policy files would come from the default policy. A repository whose entry has merged to `develop` but not yet been promoted is configured from a hub checkout at `develop`. Every other run stays on `main`. Both modes need project access on the token as well as admin on the repository, since the project link is read and written through GraphQL. A token that cannot read projects stops `apply` in its pre-flight with nothing written, naming the scope to grant. Under `check` such a token reads as a failing project group. A token that can read them but not write one reaches the link write itself and fails there. The script applies `settings.json`, the Dependabot security features, the label set, both rulesets, the link to the fleet project, and the repository's secrets from `~/.secrets/`. The link is written only where it is absent. The declared project title is compared with the live one first. The comparison exists because a project number is reusable and the same declared number is applied to every fleet repository. A registered repository can omit the model and use the registry lookup. The model is passed as:

```sh
repo-config/configure.sh apply owner/repo release|operational
```

Then validate the result with `repo-config/configure.sh check owner/repo release|operational`, run from the same checkout. The command asserts every applied ruleset, setting, label, security feature, and the fleet project link, plus the deployment environments described below. It exits non-zero on drift (the settings checks are driven by the committed payloads, and the ruleset checks by the payloads composed for the repo). Or import each ruleset by hand from the payloads `configure.sh` composes for the repo. Write them with `uv run --no-project --script scripts/agent_policy.py rulesets --registry registry/repos.json --name <repo> --model <release|operational> --out-dir <dir>`. Then run `gh api -X POST repos/<owner>/<repo>/rulesets --input <dir>/<name>.json` for each of `develop` and `main`. `gh ruleset` is read-only, so creation goes through `gh api`. [`repo-config/README.md` "Rulesets"][repo-config-readme-rulesets] says what the composition changes. The required check binds by name and only turns green after the repo's PR workflow runs once. To edit a live ruleset, GET it, change the field, and PUT the whole writable subset back (a partial PUT `422`s).

## Central Bot Settings

CodeRabbit reads a repository named `coderabbit` under the owner as central configuration, and Qodo reads `pr-agent-settings`. Each holds a README and one config file that turns automatic review off account-wide. A repository carrying its own `.coderabbit.yaml` uses that file instead. A repository carrying its own `.pr_agent.toml` overrides each key that file sets.

`scripts/central_config.py` writes both repositories from the hub. The READMEs are hand-authored sources under `repo-config/central/`, and the config text is a constant in the script. Each settings repository has a registry entry with `status: "excluded"`, so the fleet audit expects no baseline from it.

A central file with automatic review off silences the bot on every repository that lacks its own file. So the config file exists only while every live registry repository under the owner carries its own file. Each repository's own file sits on its default branch and sets the same key to a bool. A live repository is any entry that is neither archived nor excluded, and a registry listing none under the owner refuses rather than passing. That key is `reviews.auto_review.enabled` for CodeRabbit and `config.disable_auto_feedback` for Qodo. An earlier central file without this gate silenced CodeRabbit on the repositories that had no file of their own yet.

- `precondition` prints `ok` or `FAIL` per repository. It exits 0 when the gate holds, 1 when it does not, and 2 when a read could not run.
- `write` puts the README, and the config file only while the gate holds, into a checkout. It removes a config file the gate no longer allows and prints a `withheld` line naming the repositories that lack their own file. It never stages or commits, and `--dry-run` writes nothing.
- `check` reads the settings repository at a ref, `main` by default, and reports drift from what `write` produces. A config file present while the gate fails is drift, and one withheld while the gate fails is not.

```sh
uv run --no-project --script scripts/central_config.py precondition --repo ptr727/coderabbit --registry registry/repos.json
uv run --no-project --script scripts/central_config.py write --repo ptr727/coderabbit --registry registry/repos.json --checkout <checkout of ptr727/coderabbit>
uv run --no-project --script scripts/central_config.py check --repo ptr727/pr-agent-settings --registry registry/repos.json --ref main
```

Carrying the result into a settings repository is a pull request there, and it needs the maintainer's go-ahead. `write` reads the gate once, and the file takes effect only when that pull request merges. So run `check` just before the merge and again after it. Run it again whenever a repository joins the registry.

## Deployment Environments

`check` asserts that every environment the registry entry's `environments` declares exists and carries the declared deployment-branch policy, and `apply` writes none. The asymmetry is deliberate. An environment's secrets and variables are credentials that exist nowhere in the hub. So an `apply` could create the environment and set its policy and still leave it unable to publish, while reporting success. Their names are enumerable through the API, and their values in the case of a variable. But no tool here queries those stores. That is a choice about what this script owns rather than a limit the API imposes. The policy is checkable on its own and worth checking, because it decides which refs may deploy at all. On an OIDC publish that is half the gate: a ref the policy excludes mints no token.

Each entry names the environment and one of two `branchPolicy` forms. `custom` also declares `branches`, the exact set the environment allows, and the check compares the live set to it in both directions. Branches are the whole vocabulary a `custom` entry has. GitHub's custom policies admit a tag entry too, and one has no declaration here. So an environment allowing a tag under a `custom` policy reads as drift until this field grows a way to say so. `none` admits every ref and declares no branch set. So a repository whose gate lives in its deploy workflow instead records `none`. It then gets a policy appearing later read as a change rather than as the gate arriving.

GitHub offers a third form, protected branches only, and this field deliberately does not. It counts *classic* branch protection rules, which this document has already told you to remove in favor of rulesets, and no fleet repository has one. Declaring it would assert a gate that does not exist, and a live environment set to it fails the check as the drift it is.

```json
"environments": [{ "name": "pypi", "branchPolicy": "custom", "branches": ["develop", "main"] }]
```

A repository declaring no environment has nothing asserted against it, though the live list is still read and an unreadable one fails the run. An environment the registry declares nothing about is reported rather than asserted. This is because GitHub creates some without being asked (the Copilot coding agent's is the one every fleet repository has). What no tool covers either way is whether an environment's secrets and variables are present and valid. So read a clean run as a statement about each declared environment existing and carrying the policy declared for it. Never read it as a statement about whether the deploy can succeed.

## Regenerating the Payloads

To change the canonical rulesets, edit the live rulesets of a fleet repository carrying the full payloads, then regenerate the committed files from it. Its policy places the Copilot rule on `main`, and its registry entry does not declare `pullRequestCheck: false`. A repository failing either test regenerates files missing the Copilot rule or the required status check. The hub fails the first wherever its own policy drops the Copilot rule. `configure.sh` then refuses to compose for any repository wanting the rule the files lost. Set `repo` to the chosen repository:

```sh
model=release # Set to release or operational for the payload set being regenerated.
repo="<owner>/<repo>" # A repository whose live rulesets are the full payloads, per the paragraph above.
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

`Require signed commits` rejects any pre-existing unsigned commit, so the first `develop -> main` release on a repo with unsigned history is blocked. Re-signing that history is a non-fast-forward that the `Block force pushes` rule rejects, **and the admin bypass does not cover `git push --force`**. Completing it requires temporarily disabling the ruleset and a maintainer force-push. This is a one-time, maintainer-performed migration that deliberately uses the force-push [GOVERNANCE.md "Git and Commit Rules"][governance-git-and-commit-rules] forbids agents from running. **An agent must never execute it, and must surface it to the maintainer instead.** Greenfield repos where signing is live before the first commit never hit this. When the rewrite touches commits committed under a bot or web-flow identity (`dependabot[bot]`, `github-actions[bot]`), set each commit's committer to the signing identity before re-signing. That way the committer GitHub verifies matches your key, rather than leaving your key over another identity's commit. See the history-rewrite rule in [GOVERNANCE.md "Git and Commit Rules"][governance-git-and-commit-rules].

<!-- Repo -->

[governance-git-and-commit-rules]: ../GOVERNANCE.md#git-and-commit-rules
[repo-config-readme-provisioning]: ../repo-config/README.md#provisioning-from-secrets
[repo-config-readme-rulesets]: ../repo-config/README.md#rulesets
