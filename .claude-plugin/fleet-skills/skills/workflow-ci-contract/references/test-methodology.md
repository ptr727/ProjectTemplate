# Testing a Repo's Workflows

The three escalating verification modes from `WORKFLOW.md` section 5, which keeps authority. N/A items (a check or scenario for an absent construct) are recorded and excluded, never failed.

## 5A: Static Audit

Read the workflow files, `version.json`, and whatever else a check names as its own evidence: a project or dependency file, `global.json`, `codecov.yml`, `.gitignore`, the branch ruleset, and the repo's Actions and Dependabot secret names. Assert the structural fact behind each applicable D-guarantee, each pass, fail, or N/A with a `file:line` citation, cite a repository setting by its own name where that setting rather than a file is the evidence, and remember the two layers, asserting each input in the layer that declares it. `WORKFLOW.md` 5A carries the whole core list and the per-type addenda, and the sibling `d-guarantees.md` carries the guarantees each item answers to, so read this as an index into them rather than as the sweep itself.

The core sweep reaches the paths-filter, naming each target's own build paths so a change touching none marks nothing. It reaches smoke gating on every upload. It reaches the aggregator's `needs:` and its skip and fail handling. It reaches coverage collection and its best-effort Codecov upload in every C# and Python repo that has tests, since a repo whose coverage never reaches Codecov passes every other check in this list. It reaches the entry validation jobs and the two-directional release gate. It reaches the single-branch NBGV classification, with the gate's default-branch literal, the `prerelease` expression, and `version.json`'s `publicReleaseRefSpec` all naming the repo's actual default branch. It reaches `target_commitish` from `GitCommitId`. It reaches the consume-then-delete artifact lifecycle, with `retention-days: 1` everywhere and no blanket delete. It reaches the `pattern:` handoff and `inputs.branch` config. It reaches the publisher's serialized concurrency and the SHA pins. Those are entry points into `WORKFLOW.md` 5A's core list rather than the whole of it.

The per-type addenda cover .NET publish, NuGet, PyPI, Docker, and a static site deployed to a host, several assertions each. Apply only the ones the repo's types imply, and read them in `WORKFLOW.md` 5A rather than from this list.

## 5B: Trace Scenarios

For each applicable scenario, evaluate every job's `if:`/`needs:` against the inputs and compare the predicted run/skip, version, release, and artifact end state to the expected table in `WORKFLOW.md` 5B. The load-bearing ones:

- **S1** a PR touching a target: that target smoke-builds, nothing uploads, the aggregator succeeds.
- **S5/S6** a bot push to `main`: publishes only when code-affecting, and a human push never does.
- **S7** a publish run builds the one trigger branch with the right classification and leaves no dangling artifacts.
- **S8** a dispatch from a ref other than `main`/`develop` fails fast.
- **S9** a no-op re-run on a schedule or push trigger: release-create skipped, registries dedupe, package build artifacts still deleted, Docker still re-pushes. A dispatch re-run refreshes the release instead.
- **S10** branch and version classification disagree: the gate fails loud and everything downstream skips.
- **S12/S13** a deploy dispatch: ref gate first, environment re-asserted, pointer flip separate, live check names the release, and a production deploy from a non-default ref fails before anything is written.

## 5C: Live Probe

Only for what a static trace cannot settle. Every probe that dispatches a workflow, re-runs a real publish, or acts on the deploy host directly is the maintainer's to run: the agent prepares the command and reads the result back afterwards, and a harness refusal to fire one is the control working, never something to re-shape. The probes are a trivial PR to confirm S1, which runs same-repo only wherever the repo has a Docker leg, since that leg logs in to the registry even on smoke, registry queries after a real publish, the version classification and artifact lifecycle read from a real publish's logs, and the deploy ref gate, which is verified only by tripping it. That gate's evidence is four items, the gate job's conclusion, its error text naming the expected and the received ref, every downstream job recorded skipped rather than passed, and the production environment's deployment list carrying no deployment from the dispatched ref, because a gate that fails open and a gate nobody tripped leave the same empty run history behind.

## Verdict

Record the workflow operational when every applicable 5A item passes, every applicable 5B scenario's predicted output equals the expected, and no 5C probe that was run contradicts either. Any applicable mismatch is a defect. The verdict names the failing guarantees with the triggering input for each, the items recorded N/A, and the 5C probes prepared but not run, so a static-only audit and a fully probed one do not read alike. Per-project-type walkthroughs mapping scenarios onto targets, including source-only, static-site, and operational shapes, are `WORKFLOW.md` section 6.
