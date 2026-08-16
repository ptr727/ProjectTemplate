# Release Build and Publish Mechanics

Full detail for the "Publishing" rules in `SKILL.md`. Load this when adding or removing a release
target, wiring a new leaf build task, deciding where a build output belongs (a GitHub Release
asset, a package-registry push, an image push, a deploy), or setting up a wrapper repo that tracks
an upstream release, not for reading the release model's shape (the SKILL.md summary covers that).

## Reusable-task parameter contract

Every `build-*-task.yml` and `build-release-task.yml` takes `ref` (git ref to check out/version),
`branch` (logical branch driving config/tags/prerelease, where `main` => Release/`latest`/
non-prerelease, else Debug/`develop`/prerelease), and where relevant `smoke`.
**Branch-derived config keys off `inputs.branch`**: each run builds one branch, and the top-level
publisher passes `branch: ${{ github.ref_name }}`, which the tasks forward and read as
`inputs.branch` (not `github.ref_name`) for config/tags/prerelease. `get-version-task.yml` takes a
`ref` so NBGV versions the right branch.

## Per-target subsetting

`build-release-task.yml` has per-target `enable_*` gates and self-contained leaf tasks, so a
project that drops a target deletes: its `build-<target>-task.yml`, the matching job plus
`github-release`'s `needs` entry in `build-release-task.yml`, its path-filter entry in
`test-pull-request.yml`, and (for PyPI) the `publish-pypi` job in `publish-release.yml`. CodeGen,
versioning, badge, merge-bot, and Dependabot are target-agnostic.

## Orchestration vs. build: the override seam

The pipeline splits into two layers. The **orchestration** layer is generic and is the
standardization baseline: `publish-release.yml` (single-branch publish plan), the `get-version`
plus `github-release` jobs inside `build-release-task.yml`, `get-version-task.yml`, and the
aggregator shape of `test-pull-request.yml`. Within
`test-pull-request.yml`, only the `changes -> smoke-build -> check-workflow-status` aggregator
wiring and the ruleset-bound job name are verbatim orchestration, while the `unit-test` job and
the `dorny/paths-filter` entries are owned/per-target. The **build** layer (the
`build-<target>-task.yml` leaf tasks) is what a derived project owns and replaces.

The contract that keeps the seam clean: **a target contributes files to the GitHub release by
uploading a workflow artifact named `release-asset-<branch>-<target>`.** The `github-release` job
collects every `release-asset-<branch>-*` artifact by pattern, so its `download-artifact` step
uses `pattern:`/`merge-multiple:`, **never an `artifact-ids:` that names a build job's output**
(the producing build jobs still appear in `needs` for sequencing). That makes the tag-the-commit
plus create-the-release plus attach-the-assets logic reusable **as-is** across repos. **This
name-pattern handoff is canonical for every repo, single-target included**: name your one asset
`release-asset-<branch>-<target>` and the verbatim `github-release` globs it. Do not switch a
single-target repo to an `artifact-id` output plus `download-artifact` `artifact-ids:`, which
looks tidier for 1:1 but forks the `github-release` download and breaks its verbatim carry.

**What a repo still curates** (by design, not a leak): the *list* of leaf jobs in
`build-release-task.yml`. Per the per-target subsetting rule above, delete the target jobs not
shipped and add the ones that are. `build-release-task.yml`'s `github-release` job is untouched, but the file
is not byte-identical because its `needs`/job list reflects the repo's own targets. Making that
list itself target-agnostic is the release-chain phase of `docs/reusable-workflows.md` in the
hub, where the orchestrator becomes a hub-hosted task and each target a composite-action hook,
and until that phase ships the list stays per repo.

## Map your outputs to the right seam

Pick by where each artifact *goes*, not by language:

- **Files attached to the GitHub Release** (zips, binaries, packaged libraries): one leaf task per
  output, each uploading `release-asset-<branch>-<name>`. A data-only repo (e.g. a symbol library)
  has exactly one such task: validate -> `zip` -> upload `release-asset-<branch>-library`. It
  deletes the nuget/pypi/executable/docker jobs and the `publish-pypi` job, keeps `github-release`
  as-is. This is also where the .NET `build-executable-task` lives, and it is *not* a generic file
  step but specifically `dotnet publish` of the console app, so replace it wholesale, don't adapt
  it.
- **Package-registry pushes** (NuGet.org, PyPI): the leaf task both builds **and** publishes to
  its registry. NuGet pushes from inside `build-nugetlibrary-task` (`dotnet nuget push
  --skip-duplicate`) *and* also uploads a `release-asset-*` (.7z) for the GitHub release. PyPI is
  split: `build-pypilibrary-task` only builds and uploads the `pypilibrary-build-<branch>`
  artifact, and the separate `publish-pypi` job in `publish-release.yml` does the OIDC
  Trusted-Publishing upload (`id-token: write` is granted only at that one entry point), and PyPI
  contributes **no** `release-asset-*`.
- **Image-registry pushes** (Docker Hub): `build-docker-task` pushes multi-arch tags directly and
  contributes **no** `release-asset-*`. The image tag is build-layer-owned, so drive it from
  whatever version source fits (NBGV `SemVer2`, an upstream-release pin, or a per-image matrix).
  To publish the Docker Hub repository overview, the hub-hosted `publish-docker-readme-task.yml`
  pushes a readme via `peter-evans/dockerhub-description` (single-repo by default, matrix per
  image for multi-image repos), wired into `publish-release.yml` and gated to `main` both by the
  caller's `branch` input and inside the task itself. A `docker-readme-transform` hook sets a
  `readme-filepath` step output naming which file to push, defaulting to `Docker/README.md` if
  present else `README.md` as-is, so a repo needs a hook only to render the file first or to
  override that default.
- **Filesystem on a host the project owns** (a static site, a config tree): a deploy leaf builds
  the tree and ships it over the repo's own transport, contributing **no** `release-asset-*`. It
  is a **separate `workflow_dispatch`** from the release, so a redeploy of an unchanged commit
  mints no tag, and its credentials come from a **per-environment GitHub Environment** rather than
  the repository secret store. Its last step asserts what the host actually serves, the release id
  and the environment, never that the transport exited zero. Retention at the destination is
  bounded by a declared count, and one side is recorded as owning the prune: the deploy where its
  credential can observe the destination, the host where that credential is deliberately
  write-only.
- **Source-only / no build** (validate + tag + release): this seam does not apply. A source-only
  repo carries **no** `build-release-task.yml` (its `appliesTo` excludes it), so there are no leaf
  tasks and no `get-version`/`github-release` jobs to curate. Its whole release is
  the standalone `publish-release.yml` on `workflow_dispatch`: a `validate` job (the repo's
  reusable validation task) gates a publish job that **inlines** NBGV for the tag and
  `action-gh-release` for the release (tag, auto source archive, README, LICENSE).

`get-version-task.yml` installs the .NET SDK only because NBGV needs the runtime to compute the
version/tag, which is heavyweight but expected even for a non-.NET repo, and acceptable as-is.

## No-op republish guarantee

A weekly/dispatch publish where NBGV `SemVer2` is **unchanged** (no new commit since the last
publish) re-pushes **nothing** to GitHub Releases (the `github-release` job's `release-exists`
check skips the create step), NuGet (`dotnet nuget push --skip-duplicate`), or PyPI
(`gh-action-pypi-publish` `skip-existing: true`), since all three key on the version string.
**Docker always re-pushes** by design: it picks up upstream base-image refreshes (e.g.
`ubuntu:rolling`) that aren't visible in the repo. Boundary: `version.json` has **no
`pathFilters`**, so *any* commit, including a CI/workflow-only or docs-only change, advances the
NBGV git height and therefore `SemVer2`, and the next publish *does* create a fresh release for it
even when the shipped binary is byte-identical. This is accepted NBGV behavior, and `pathFilters`
are intentionally not added.

## Wrapper repos that track an upstream release

A repo wrapping an upstream release uses the hub-hosted `check-upstream-version-task.yml`: a
required `resolve-upstream` hook sets a `versions` step output, a **JSON object of
`name -> version`**, written to a committed state file at the **repo root beside `version.json`**
(default `upstream-version.json`, since it is a build-input version source, not GitHub-platform
config, so it does not belong under `.github/`), and opens a rolling App-signed bump PR per branch
that the merge-bot auto-merges (`merge-upstream-version`). The object carries one key for the
common single-version case (`{"version": "X"}`) or N keys for a wrapper that pins several upstream
components (e.g. an image plus a companion tool), and the build reads each component by key, and
the bump PR's title/body name only the keys that actually moved. Call it from a scheduled
entry-point workflow and matrix only the branches that ship the version (a CI-only version uses
`["develop"]`). A merged bump ships on the **next publish**, not immediately, which is the
two-phase latency tradeoff. A tracker whose bump needs a human decision instead of auto-merge, for
example one that snapshots a package list to review rather than a version to adopt outright, sets
`auto-merge: false`, which prefixes the head so no merge-bot rule matches it.
