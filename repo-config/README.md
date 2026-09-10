# repo-config

Hub-only repository and branch configuration held as committed files, kept out of `.github/` (which holds the GitHub-consumed configuration: workflows, Dependabot). Downstream repositories carry no `repo-config/` directory. Apply and check commands run from a hub checkout at `main` and name the target repository.

- `main.json`, `develop.json`, and `operational/develop.json`: the canonical branch rulesets as the managed part of the writable API subset (`name`, `target`, `enforcement`, `conditions`, `rules`). `main.json` is shared. `develop.json` serves release repos, and `operational/develop.json` serves operational repos. `repo-config/configure.sh check owner/repo release|operational` compares the selected payloads with the live rulesets. `bypass_actors` is writable and deliberately unmanaged, so no payload declares one and nothing diffs it: who may bypass a ruleset is a human decision taken in the UI, which `repo-config/configure.sh` preserves on `apply` and reports without asserting on `check`.
- `labels.json`: the fleet label set, one `name`, `color`, and `description` per label. `repo-config/configure.sh apply owner/repo release|operational` creates or updates every declared label by name and deletes nothing, so a label a repo adds of its own stays. `check` asserts each declared label on all three fields and reports the undeclared ones without judging them.
- `configure.sh`: run from a hub checkout at `main`, per [GOVERNANCE.md "Hub-Hosted Tooling"][governance-hub-hosted-tooling]. It resolves every payload path against the hub's `repo-config/` directory. Name the target repository explicitly, since the command defaults to whichever repository the shell is sitting in. `repo-config/configure.sh apply owner/repo release|operational` creates or updates the settings, Dependabot security features, labels, and rulesets idempotently. `repo-config/configure.sh check owner/repo release|operational` is the read-only counterpart and exits non-zero on drift. It is not an exact inverse: it also asserts that every environment the registry's `environments` declares for the repo exists and carries the declared deployment-branch policy, neither of which `apply` writes, for the reason [docs/repo-config.md][repo-config-doc] "Deployment Environments" gives. The model defaults to the registry `workflowModel` lookup. Pass it explicitly for a repository outside the registry.

## Rulesets

Two workflow models share `main.json` but differ on `develop` (registry `workflowModel`, default `release`):

- **`release`** (`develop.json`): `develop` requires squash merges with linear history and a PR, the feature-branch pipeline.
- **`operational`** (`operational/develop.json`): `develop` takes **direct signed pushes**, carrying only `deletion`, `non_fast_forward`, and `required_signatures`; no PR, no status-check, no Copilot-on-push. CI runs on the push as advisory feedback. Read the dropped rules as an allowance rather than a prohibition, since a PR into `develop` remains legal and the lint workflow triggers on it, with its result reported and not required (a required check here would gate the direct push as well). This is for live-service config repos that edit `develop` directly and promote a known-good snapshot to `main` via an occasional PR (see [GOVERNANCE.md "Branching Model"][governance-branching-model]).

`main` (both models) requires merge-commit merges (no linear-history rule), signed commits, a passing `Check pull request workflow status job`, resolved review threads, and Copilot review, and blocks force-pushes and deletion, so a `develop -> main` promotion is always gated even when `develop` takes direct commits. Every ruleset intentionally leaves "Require branches to be up to date before merging" **off**, per [GOVERNANCE.md "Branching Model"][governance-branching-model].

The result is **exactly two rulesets named `develop` and `main`**, and the names are load-bearing (`GOVERNANCE.md` and the workflows reference them). Only the `develop` *content* varies by model. The required check binds by name and only turns green after the repo's PR workflow runs once.

## Secrets

Publish credentials required per mechanism are enumerated in `spec/secrets.json`. A repo needs only the mechanisms its own publish targets use, so a source-only repo needs none of the publish credentials below. NuGet and PyPI use keyless OIDC Trusted Publishing (no stored key, so the publish job needs `id-token: write`, and PyPI additionally an `environment: pypi` gate). That publish job belongs to the repo's own workflow file, since trusted publishing validates the OIDC token's `job_workflow_ref` claim against the repository owning the package and rejects a reusable workflow's ref, so `id-token: write` is granted at that one entry point and nowhere else. The registry-side policy is the other half of that pairing and is configured on nuget.org or PyPI rather than here: it names the repository and the workflow file the push runs from, so moving the push between workflow files means repointing the policy in the same change. Docker Hub has no OIDC equivalent and uses a stored `DOCKER_HUB_USERNAME` + `DOCKER_HUB_ACCESS_TOKEN` in both the Actions and Dependabot secret stores. Codegen and merge-bot repos add a GitHub App (`CODEGEN_APP_CLIENT_ID` + `CODEGEN_APP_PRIVATE_KEY` in both stores, and the app must be installed, not just created). App-token call sites use `client-id`, never the deprecated `app-id`.

## Labels

The triage labels classify an issue by the kind of work it needs, so a backlog sweep can pick the gates and scripts, which converge, ahead of the prose defects, which re-enter the review loop when worked one bundle at a time. An issue recording work to be done carries exactly one of these four, or `enhancement` for a feature, beside whatever surface labels it also carries. An issue holding nothing but a question for the maintainer records no such work, so it carries no triage label at all.

- **`gate`**: a rule with no mechanical check, or a check that misses a shape.
- **`script`**: a defect in hub tooling.
- **`prose`**: a defect in rule or procedure text.
- **`chore`**: registry, labels, rollout, and other fleet housekeeping.

`decision` is a marker rather than a fifth triage label. When it goes on an issue, and when it comes off again, are set by [GOVERNANCE.md "Communicating with the User"][governance-communicating-with-the-user], which is also what reads the label back, so nothing here decides either.

- **`decision`**: a question waiting on the maintainer, alone or beside whatever else the issue carries.

`handoff` is a surface marker like `agents` and `skills` rather than a triage label, and a handoff issue carries no triage label at all, because it records work to do next rather than work of its own. Without that stated here, the next reader of this taxonomy reads a handoff issue carrying no triage label as drift. The chain it indexes is found by this label rather than by a title search, which is what lets a handoff title carry a human subject with nothing parsing it, and every enumeration that ranks or counts the open backlog excludes the label for the same reason one is always open by design.

- **`handoff`**: a link in the session handoff chain, one open issue per track.

The class labels `introduced` and `pre-existing` record which class, per the `local-strict-review` Skill's "Disposing of Findings", a filed review finding carried. A `style` finding is declined rather than filed, so it has no label. `agents`, `skills`, and `codegen` mark the surface, and the rest are GitHub's own defaults and the Dependabot pair, declared so every fleet repo carries at least this set.

## Repo Settings

The fleet-standard general settings live in [`settings.json`][settings-json] and are applied idempotently by `repo-config/configure.sh apply owner/repo release|operational` alongside the rulesets (`gh api PATCH /repos/{owner}/{repo}`). The two settings that depend on per-repo state, `has_discussions` (visibility) and `default_branch` (main-must-exist), are computed by the script, not stored in the file. `apply` also enables Dependabot vulnerability alerts and automated security updates, fleet policy applied via the API rather than a `settings.json` key. `repo-config/configure.sh check owner/repo release|operational` validates all of these and exits non-zero on drift.

- **Default branch `main`** (the script sets it only when a `main` branch exists, never pointing the default at a missing branch).
- **Merge methods**: `Allow merge commits` and `Allow squash merging` on, **rebase off**, and each branch ruleset then picks its method (merge on `main`, squash on `develop`).
- **Auto-merge on** (the merge-bot needs it) and **`Always suggest updating pull request branches` on**.
- **`Automatically delete head branches` is OFF, deliberately.** With it on, a `develop -> main` promotion (whose PR head is `develop`) would delete `develop`. There is no per-branch exemption, so the repo-wide toggle stays off to protect `develop`. **The CLI has the same trap: never `gh pr merge --delete-branch` a promotion PR whose head is `develop`**, since the explicit flag deletes `develop` regardless of this setting (see [GOVERNANCE.md "Branching Model"][governance-branching-model]).
- **Wikis and Projects off. Discussions on public repos only** (off on private). **Sponsorships off**, since the button is driven by `.github/FUNDING.yml` rather than a REST toggle, and the fleet ships none.
- **Actions / General**: allow GitHub Actions to create and approve pull requests (for the bots).

<!-- Repo -->

[governance-branching-model]: ../GOVERNANCE.md#branching-model
[governance-communicating-with-the-user]: ../GOVERNANCE.md#communicating-with-the-user
[governance-hub-hosted-tooling]: ../GOVERNANCE.md#hub-hosted-tooling
[repo-config-doc]: ../docs/repo-config.md
[settings-json]: ./settings.json
