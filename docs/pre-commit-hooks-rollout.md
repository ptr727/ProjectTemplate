# Fleet Pre-Commit Hooks Rollout

Tracks the fleet-wide local-hook posture change repo by repo. The policy itself lives in
[GOVERNANCE.md "Running the Linters Locally"][governance-running-the-linters-locally] and the
canonical configs live in [`catalog/snippets/husky/`][snippets-husky] and
[`catalog/snippets/pre-commit/`][snippets-pre-commit]. This doc is the rollout checklist only,
not a restatement of the rule. It is **hub-only** and is not carried downstream, the same way
[`docs/eol-lf-rollout.md`][eol-lf-rollout] is hub-only, because it tracks the hub's own migration
rather than a fact a downstream repo's own docs need to carry.

**Maintenance rule.** This file is hub-only, so a downstream repo's own conversion PR cannot edit
it, and the audit that judges `parity.hooks` is always hub-authored, never a repo's own report of
itself, per `AUDIT.md`. Once the hub's post-merge audit reports a repo's `parity.hooks` operational,
the hub opens that repo's own small hub-side PR, the next step of the same `resync-a-repo` pass,
and checks the repo's box below in it. A repo's box stays unchecked while that follow-up PR is
pending, which is expected, not stale. It is stale only once the audit has confirmed
`parity.hooks` operational and no follow-up PR exists to show for it.

## What Changed

`spec/project-types.json`'s `crossCutting.linter-parity` dimension gained `parity.hooks`, judged
by hand during an `AUDIT.md` run like every sibling check in that dimension, never by
`spec/audit.py` itself: a repo with no local hook mechanism wired at all is a `linter-parity`
defect, the same severity a missing markdownlint config already gets. A repo mid-convergence, the
doc-gate half wired and the language-format half not yet because its own corpus does not pass it
clean, stays operational, per the existing carve-out in GOVERNANCE.md. Two catalog snippets carry
the canonical shape: Husky.Net
(`catalog/snippets/husky/`, for .NET or any project including Python) and the Python `pre-commit`
framework (`catalog/snippets/pre-commit/`, for a repo with no `.husky/` tree). Both now carry the
same shared doc gates, the diff-scoped prose/comment-style gate and the whole-tree line-ending
check, fetched fresh from the hub's `main` branch at run time via `hub-fetch-run.py` rather than
vendored or pinned.

## Per-Repo Conversion

Copy and adapt the applicable catalog snippet plus `catalog/snippets/hub-fetch-run.py`.
Enable it (`git config core.hooksPath .husky`, or `uv tool install pre-commit` once then
`pre-commit install`). For the Husky.Net snippet specifically, also run `dotnet tool restore`
then `dotnet husky install`, which generates `.husky/_/husky.sh`, the file the hook sources.
Confirm the doc gates run clean against the repo's current tree.
Open the PR through the repo's normal branching model.
A repo whose corpus does not yet pass its language formatter clean ships the doc-gate half first
and adds the language half once it does, per the mid-convergence carve-out. That partial state is
not a reason to leave the box unchecked, since the doc-gate half alone already satisfies
`parity.hooks`' intent tier. After the downstream PR merges and the hub's post-merge audit
confirms `parity.hooks` operational, open the small hub-side PR the maintenance rule above
describes and check the box below in it.

A repo declaring neither `csharp` nor `python` in `registry/repos.json` (the `eda` repos, and any
repo with no fleet-covered language) has no language-format half to add: the doc-gate half alone
is the complete, operational state for that repo, not an incomplete one a later resync should
mistake for unfinished work.

## Rollout Checklist

Repos and their current `registry/repos.json` `types`, from the hub's own registry as of this
doc's authorship. Archived repos are not tracked.

- [x] **ProjectTemplate** (`source-only`, `docs`): the hub itself. Doc gates wired in
      `.husky/pre-commit` since PR #642, and the ruff/mypy half added in this PR's own commits,
      once the corpus was confirmed clean (0 ruff errors, 200 files formatted, mypy clean).
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
