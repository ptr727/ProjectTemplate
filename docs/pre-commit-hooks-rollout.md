# Fleet Pre-Commit Hooks Rollout

Tracks the fleet-wide local-hook posture change repo by repo. The policy itself lives in
[GOVERNANCE.md "Running the Linters Locally"][governance-running-the-linters-locally] and the
canonical configs live in [`catalog/snippets/husky/`][snippets-husky] and
[`catalog/snippets/pre-commit/`][snippets-pre-commit]. This doc is the rollout checklist only,
not a restatement of the rule. It is **hub-only** and is not carried downstream, the same way
[`docs/eol-lf-rollout.md`][eol-lf-rollout] is hub-only, because it tracks the hub's own migration
rather than a fact a downstream repo's own docs need to carry.

**Maintenance rule.** Check a repo's box in the same pull request that converts it, via the
normal `resync-a-repo` procedure, once that repo's own audit reports `parity.hooks` operational.
A register showing a repo unchecked after its conversion PR merged is itself stale prose, so this
doc is only trustworthy while that rule holds.

## What Changed

`spec/project-types.json`'s `crossCutting.linter-parity` dimension gained `parity.hooks`: a repo
with no local hook mechanism wired at all is now a `linter-parity` defect, the same severity a
missing markdownlint config already gets. A repo mid-convergence, the doc-gate half wired and the
language-format half not yet because its own corpus does not pass it clean, stays operational, per
the existing carve-out in GOVERNANCE.md. Two catalog snippets carry the canonical shape: Husky.Net
(`catalog/snippets/husky/`, for .NET or any project including Python) and the Python `pre-commit`
framework (`catalog/snippets/pre-commit/`, for a repo with no `.husky/` tree). Both now carry the
same shared doc gates, the diff-scoped prose/comment-style gate and the whole-tree line-ending
check, fetched fresh from the hub's `main` branch at run time via `hub-fetch-run.py` rather than
vendored or pinned.

## Per-Repo Conversion

Copy and adapt the applicable catalog snippet plus `catalog/snippets/hub-fetch-run.py`, enable it
(`git config core.hooksPath .husky` or `uv run pre-commit install`), confirm the doc gates run
clean against the repo's current tree, and open the PR through the repo's normal branching model.
A repo whose corpus does not yet pass its language formatter clean ships the doc-gate half first
and adds the language half once it does, per the mid-convergence carve-out. That partial state is
not a reason to leave the box unchecked, since the doc-gate half alone already satisfies
`parity.hooks`' intent tier. After merge, check the box below.

A repo declaring neither `csharp` nor `python` in `registry/repos.json` (the `eda` repos, and any
repo with no fleet-covered language) has no language-format half to add: the doc-gate half alone
is the complete, operational state for that repo, not an incomplete one a later resync should
mistake for unfinished work.

## Rollout Checklist

Repos and their current `registry/repos.json` `types`, from the hub's own registry as of this
doc's authorship. Archived repos are not tracked.

- [ ] **ProjectTemplate** (`source-only`, `docs`): the hub itself, already mid-convergence, doc
      gates wired in `.husky/pre-commit` since PR #642. The ruff half stays out until this repo's
      own Python corpus passes `ruff format --check` / `ruff check` clean, tracked separately in
      `TODO.md`. Checked once that convergence lands, not as part of the downstream sweep below.
- [ ] **Utilities** (`csharp`, `nuget`): Husky.Net + doc gates.
- [ ] **LanguageTags** (`csharp`, `nuget`, `codegen`): Husky.Net + doc gates.
- [ ] **aiopurpleair** (`python`, `pypi`): `pre-commit` framework + doc gates.
- [ ] **homeassistant-purpleair** (`python`, `homeassistant`): `pre-commit` framework + doc gates.
- [ ] **Financial-Modeling** (`python`, `source-only`): `pre-commit` framework + doc gates.
- [ ] **PlexCleaner** (`csharp`, `dotnet-publish`, `docker`, `python`): Husky.Net (with its ruff
      block filled in) covers both languages in one hook, rather than wiring two mechanisms.
- [ ] **ESPHome-NonRoot** (`docker`, `upstream-wrapper`): doc-gate half only, no declared language.
- [ ] **VSCode-Server-DotNetCore** (`docker`): doc-gate half only, no declared language.
- [ ] **NxWitness** (`docker`, `upstream-wrapper`, `codegen`, `csharp`): Husky.Net + doc gates.
- [ ] **HomeAutomation-Config** (`source-only`): doc-gate half only, no declared language.
- [ ] **KiCadLibrary** (`eda`): doc-gate half only, no fleet-covered language.
- [ ] **EspDinIoT** (`eda`): doc-gate half only, no fleet-covered language.
- [ ] **ESPHome-Config** (`source-only`, `python`, `cpp`): `pre-commit` framework + doc gates for
      the Python half. `cpp` has no fleet linter declared today, out of scope here.
- [ ] **HomeAssistant-Config** (`source-only`): doc-gate half only, no declared language.
- [ ] **DevKitCIoT** (`eda`): doc-gate half only, no fleet-covered language.
- [ ] **PhotoCleaner** (`csharp`, `dotnet-publish`, `docker`): Husky.Net + doc gates.
- [ ] **MediaTools** (`csharp`, `nuget`): Husky.Net + doc gates.
- [ ] **AudioCleaner** (`csharp`, `dotnet-publish`): Husky.Net + doc gates.
- [ ] **Vantage-Config** (`source-only`): doc-gate half only, no declared language.
- [ ] **HolidayLights** (`source-only`): doc-gate half only, no declared language.
- [ ] **Blog** (`hugo`, `source-only`): doc-gate half only, no declared language.

<!-- Repo -->

[eol-lf-rollout]: ./eol-lf-rollout.md
[governance-running-the-linters-locally]: ../GOVERNANCE.md#running-the-linters-locally-known-working-invocations
[snippets-husky]: ../catalog/snippets/husky/README.md
[snippets-pre-commit]: ../catalog/snippets/pre-commit/README.md
