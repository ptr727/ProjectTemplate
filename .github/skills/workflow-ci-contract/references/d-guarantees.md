# The D-Guarantees, Condensed

Each guarantee is a MUST from `WORKFLOW.md` section 4, stated as the output a conforming pipeline produces. In that section an item names an input only where the guarantee applies to a particular trigger or state, and names the failure it prevents only where the output does not already show it. An item naming neither still binds every repo whose shape its domain covers, and a workflow violating any applicable guarantee is not operational. This is the condensed catalog for working from, and `WORKFLOW.md` keeps authority: read the section there when a guarantee's exact wording decides a verdict, since a condensed item can be shorter than the one it condenses.

## D1: PR Fast-Feedback (Smoke)

- **D1.1** Only changed targets build: each target has a paths-filter entry naming the paths it is built from, unchanged targets skip, and a change touching no target's paths marks nothing. A filter written as a negation of what must not build marks a docs-only change as a target change and fails this item. Prevents a changed target slipping through unbuilt.
- **D1.2** A validation job always runs on any PR: the caller's own job reaching the reusable validator, named `validate` in every shipped stub, which is the name the aggregator `needs:`. The validator's internal jobs are not addressable from a caller, and one of the hub's is itself called `validate`, so the matching name in a `needs:` list is always the caller's own job. It detects the tree rather than the language, so a non-.NET repo calls the same validator. A repo whose validation it cannot express replaces the call (never deletes it) and re-points the aggregator's `needs:`. `smoke-build` `needs:` the `changes` job, not the validation job. Prevents a PR merging with no validation, or a dangling `needs:` that stops the whole workflow from loading.
- **D1.3** Smoke never publishes and never uploads: full compile/lint/test, no pushes, every `upload-artifact` gated on smoke being false, `!inputs.smoke` at the workflow layer and `inputs.smoke != 'true'` in a composite action, whose inputs are strings. Prevents a PR publishing and orphaned artifacts.
- **D1.4** A PR changing only `.github/workflows/**` is not smoke-built, since an inclusion list satisfying D1.1 matches no workflow path, and actionlint still validates them.
- **D1.5** One required aggregator gates merge: `if: always()`, `needs:` the validation job plus the `changes` and `smoke-build` jobs wherever the repo has a smoke build, passes on skipped smoke, blocks on failure or cancelled, and its name is ruleset-bound (job `name:` equals ruleset `context:`, renamed together).
- **D1.6** Coverage reports to Codecov for C# and Python repos with tests, a lint-only profile for that type excepted, best-effort so an outage never reds the gate, with a `codecov.yml` setting statuses informational and `.gitignore` excluding coverage output.

## D2: Validation at Entry

- **D2.1** A dedicated entry job asserts each cross-input invariant before expensive work, downstream jobs `needs:` it.
- **D2.2** The release gate fails loud when the default branch carries a prerelease suffix or a non-default branch carries none, strips `+buildmetadata` first, and on smoke skips the check while the job still succeeds (a job-level `if:` would skip dependents with it).
- **D2.3** A dispatch publish from any ref other than `main` or `develop` fails fast.
- **D2.4** Mutually-exclusive or must-pair inputs are validated, a half-filled combination fails fast.

## D3: Versioning and Classification

- **D3.1** One branch per run: `github.ref` names the built branch, NBGV classifies it directly, no `IGNORE_GITHUB_REF`.
- **D3.2** Default branch yields `X.Y.Z`, every other branch `X.Y.Z-g<sha>`, and the default-branch literal in the gate, the `prerelease` expression, and `version.json`'s `publicReleaseRefSpec` all name the repo's real default branch.
- **D3.3** `version.json` sets the major.minor floor, NBGV appends git height as the patch, and both are retained even by a no-compiler repo, since they own the tag.
- **D3.4** Registry versions follow the classification per registry: NuGet.org derives prerelease from the SemVer2 suffix, PyPI builds from `AssemblyFileVersion` with `.dev0` appended on `develop` only, and the develop build stays `--pre`-selectable above the released version.
- **D3.5** A wrapper repo drives its image version from a committed `name -> version` state file, and the leaf must actually read it, since a leaf still tagging off NBGV means the wrapper is not pinned to upstream.

## D4: Release and Publish

- **D4.1** Gated single-branch publish: a human merge never auto-publishes, the `plan` job decides once, publishes come from a code-affecting bot push to `main`, a dispatch of `main`/`develop`, or the main-only weekly Docker schedule.
- **D4.2** `target_commitish` is the built commit's SHA (NBGV `GitCommitId`), never a branch name and never a separately re-resolved ref.
- **D4.3** Every release is a tag plus source zip, README, and LICENSE, `prerelease` equals `branch != default`, file targets attach `release-asset-*`, and a no-file-target caller (Docker-only, PyPI-only, source-only) passes `expect_release_assets: false` or the release-create step fails on unmatched files, a source-only one setting every `enable_*` input false with it. A NuGet caller is not one of those, since its leaf uploads a `release-asset-*` carrying the package.
- **D4.4** No-op republish on a schedule or push trigger: an unchanged version re-pushes nothing and the release-create skips when the tag exists, while a dispatch re-run refreshes it and runs the paired asset delete with it, registries dedupe server-side under `dotnet nuget push --skip-duplicate` and PyPI's `skip-existing: true`, and Docker always re-pushes by design.
- **D4.5** A failed build blocks every publish target: `github-release` needs every build and the terminal registry pusher (Docker) needs every other build, both guarding `!failure() && !cancelled()` so a disabled or unchanged target, skipped rather than failed, still lets the release be cut and the image pushed, and a package target's separate `publish-<target>` job `needs:` the release-task call, so no build failure ships anything partial. A failed **package** push is outside that: the `publish-<target>` job runs after the whole release task and so after `github-release`, and can leave a release and tag for a version the registry never received, recovered by re-dispatching the same commit rather than by cleanup.
- **D4.6** A deploy check asserts which release and which environment answer, waiting for convergence to a bounded timeout, with an unreachable host reported distinctly from an HTTP status.

## D5: Resource Cleanup

- **D5.1** A cross-job transfer artifact is deleted by exact name or pattern at its point of consumption. An in-run intermediate may rely on the retention backstop.
- **D5.2** The delete runs exactly when the consumption happened: the same condition as a conditional consumer (the release create), and `if: ${{ !cancelled() && steps.<download-step-id>.outcome == 'success' }}` where the consumer is a push that always attempts, since a delete with no status-check function in its `if:` inherits `success()` and would skip on the failed push. So a no-op re-run that is not a dispatch skips the release-asset delete while the `nuget-build-*` and `pypi-build-*` deletes still run, and a dispatch re-run refreshes the release and runs the asset delete with it.
- **D5.3** Cleanup is best-effort (`continue-on-error`, tolerate a failed listing, delete all matching ids).
- **D5.4** Every `upload-artifact` sets `retention-days: 1`.
- **D5.5** Never blanket-delete the run's artifacts, which destroys diagnostics and auto-emitted build records.
- **D5.6** A durable deploy destination's retention is bounded by a declared count with one side recorded as owning the prune: the deploy where its credential can observe the destination, the host where the credential is deliberately write-only.

## D6: Seam Conformance

- **D6.1** The release job downloads by `pattern:`/`merge-multiple:`, never `artifact-ids:`, canonical for single-target repos too.
- **D6.2** Branch-derived config reads `inputs.branch`, never `github.ref_name`.
- **D6.3** Artifact names are branch-suffixed.
- **D6.4** A target add or drop updates the whole surface together: `enable_<target>` input, `build-<target>` job, its `github-release` and `build-docker` `needs:` entries, paths-filter entry and output, the `smoke-build` enable-forward, and a package target's separate `publish-<target>` job.

## D7: Concurrency, Permissions, Safety

- **D7.1** The publisher serializes: global ref-independent concurrency group, `cancel-in-progress: false`.
- **D7.2** A reusable job declares `permissions:` only where every caller grants that scope at startup (the block is validated before `if:`), and otherwise declares none and runs under the calling job's grant, a callee's extra scope granted by the caller at the one entry point needing it.
- **D7.3** Boolean inputs are declared in both trigger blocks and compared against both forms.
- **D7.4** Optional-dependency chaining allowlists `success`/`skipped` explicitly, beside a status-check function, since the implicit `success()` is false the moment any `needs:` job skipped.

## D8: Bots and Automation

- **D8.1** The merge-bot enables auto-merge on `opened`/`reopened` for every Dependabot tier, dispatches squash or merge by base ref, disables on a maintainer-pushed `synchronize`, and keys concurrency on the PR number, not `github.ref`.
- **D8.2** Codegen runs a deterministic matrix over both branches, Dependabot targets both branches.
- **D8.3** The upstream tracker writes a committed `name -> version` state file via a rolling per-branch bump PR the merge-bot auto-merges, and its branch prefix must match the merge-bot's head-ref pairs or auto-merge silently never fires.
- **D8.4** An identity allowlist used as a gate emits a `::warning::` on the non-matching branch rather than falling through silently, since a renamed App slug otherwise turns the gate off invisibly.

## D9: Style and Static

SHA pins with version comments, the name-suffix rules, `set -Eeuo pipefail`, `if: >-`, registry-tag Docker cache with `cache-to` only the built branch on push and `cache-from` both branches, line endings per `.editorconfig`.
