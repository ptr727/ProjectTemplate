# repo-config

Repository and branch configuration held as committed files, kept out of `.github/` (which holds the GitHub-consumed configuration: workflows, Dependabot).

- `main.json` plus one `develop` variant: the branch rulesets as the writable API subset (`name`, `target`, `enforcement`, `bypass_actors`, `conditions`, `rules`). The `develop` payload is `develop.json` (`release` repos) or `operational/develop.json` (`operational` repos). These are the canonical expected payloads that the self-audit (`AUDIT.md`) diffs the live rulesets against.
- `operational/develop.json`: the `develop` ruleset for **operational** repos (registry `workflowModel: operational`), taking direct signed pushes with no PR gate. Present in operational repos only, since a `release` repo does not have it. See "Rulesets" below.
- `configure.sh`: two modes over the GitHub API. `configure.sh apply [owner/repo] [release|operational]` creates-or-updates the settings, the Dependabot security features, and the rulesets idempotently (a full-payload update). `configure.sh check [owner/repo] [release|operational]` is the read-only inverse and exits non-zero on any drift, with the ruleset and settings assertions driven by the committed payloads so they stay repo-agnostic (rule presence, merge methods, and required checks, not a byte diff, so a GitHub-normalized stored ruleset does not false-positive). The command defaults to `apply`, the repo to the current one, and the model to the registry `workflowModel` lookup (or, absent a registry, inference from the carried `develop` payload, where an ambiguous layout aborts rather than guesses). The model may be passed as the sole positional (`configure.sh check operational`).

## Rulesets

Two workflow models share `main.json` but differ on `develop` (registry `workflowModel`, default `release`):

- **`release`** (`develop.json`): `develop` requires squash merges with linear history and a PR, the feature-branch pipeline.
- **`operational`** (`operational/develop.json`): `develop` takes **direct signed pushes**, carrying only `deletion`, `non_fast_forward`, and `required_signatures`; no PR, no status-check, no Copilot-on-push. CI runs on the push as advisory feedback. This is for live-service config repos that edit `develop` directly and promote a known-good snapshot to `main` via an occasional PR (see [GOVERNANCE.md "Branching Model"][governance-branching-model]).

`main` (both models) requires merge-commit merges (no linear-history rule), signed commits, a passing `Check pull request workflow status job`, resolved review threads, and Copilot review, and blocks force-pushes and deletion, so a `develop -> main` promotion is always gated even when `develop` takes direct commits. Every ruleset intentionally leaves "Require branches to be up to date before merging" **off**, per [GOVERNANCE.md "Branching Model"][governance-branching-model].

The result is **exactly two rulesets named `develop` and `main`**, and the names are load-bearing (`GOVERNANCE.md` and the workflows reference them). Only the `develop` *content* varies by model. The required check binds by name and only turns green after the repo's PR workflow runs once.

## Secrets

Publish credentials required per mechanism are enumerated in `spec/secrets.json`. A repo needs only the mechanisms its own publish targets use, so a source-only repo needs none of the publish credentials below. NuGet and PyPI use keyless OIDC Trusted Publishing (no stored key; the publish job needs `id-token: write`, and PyPI additionally an `environment: pypi` gate). Docker Hub has no OIDC equivalent and uses a stored `DOCKER_HUB_USERNAME` + `DOCKER_HUB_ACCESS_TOKEN` in both the Actions and Dependabot secret stores. Codegen and merge-bot repos add a GitHub App (`CODEGEN_APP_CLIENT_ID` + `CODEGEN_APP_PRIVATE_KEY` in both stores; the app must be installed, not just created). App-token call sites use `client-id`, never the deprecated `app-id`.

## Repo Settings

The fleet-standard general settings live in [`settings.json`][settings-json] and are applied idempotently by `configure.sh apply` alongside the rulesets (`gh api PATCH /repos/{owner}/{repo}`). The two settings that depend on per-repo state, `has_discussions` (visibility) and `default_branch` (main-must-exist), are computed by the script, not stored in the file. `configure.sh apply` also enables Dependabot vulnerability alerts and automated security updates, fleet policy applied via the API rather than a `settings.json` key. `configure.sh check` validates all of these and exits non-zero on drift.

- **Default branch `main`** (the script sets it only when a `main` branch exists, never pointing the default at a missing branch).
- **Merge methods**: `Allow merge commits` and `Allow squash merging` on, **rebase off**, and each branch ruleset then picks its method (merge on `main`, squash on `develop`).
- **Auto-merge on** (the merge-bot needs it) and **`Always suggest updating pull request branches` on**.
- **`Automatically delete head branches` is OFF, deliberately.** With it on, a `develop -> main` promotion (whose PR head is `develop`) would delete `develop`. There is no per-branch exemption, so the repo-wide toggle stays off to protect `develop`. **The CLI has the same trap: never `gh pr merge --delete-branch` a promotion PR whose head is `develop`**, since the explicit flag deletes `develop` regardless of this setting (see [GOVERNANCE.md "Branching Model"][governance-branching-model]).
- **Wikis and Projects off. Discussions on public repos only** (off on private). **Sponsorships off**, since the button is driven by `.github/FUNDING.yml` rather than a REST toggle, and the fleet ships none.
- **Actions / General**: allow GitHub Actions to create and approve pull requests (for the bots).

<!-- Repo -->

[governance-branching-model]: ../GOVERNANCE.md#branching-model
[settings-json]: ./settings.json
