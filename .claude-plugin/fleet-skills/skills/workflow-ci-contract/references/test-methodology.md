# Testing a Repo's Workflows

The three escalating verification modes from `WORKFLOW.md` section 5, which keeps authority. N/A items (a check or scenario for an absent construct) are recorded and excluded, never failed.

## 5A: Static Audit

Read the workflow files plus `version.json` and assert the structural fact behind each applicable D-guarantee, each pass, fail, or N/A with a `file:line` citation, remembering the two layers and asserting each input in the file that declares it. `WORKFLOW.md` 5A carries the whole core list and the per-type addenda, and the sibling `d-guarantees.md` carries the guarantees each item answers to, so read this as an index into them rather than as the sweep itself.

The core sweep reaches the paths-filter, naming each target's own build paths so a change touching none marks nothing. It reaches smoke gating on every upload. It reaches the aggregator's `needs:` and its skip and fail handling. It reaches the entry validation jobs and the two-directional release gate. It reaches the single-branch NBGV classification, with the gate's default-branch literal, the `prerelease` expression, and `version.json`'s `publicReleaseRefSpec` all naming the repo's actual default branch. It reaches `target_commitish` from `GitCommitId`. It reaches the consume-then-delete artifact lifecycle, with `retention-days: 1` everywhere and no blanket delete. It reaches the `pattern:` handoff and `inputs.branch` config. It reaches the publisher's serialized concurrency and the SHA pins.

The per-type addenda cover .NET publish, NuGet, PyPI, Docker, and a static site deployed to a host, several assertions each. Apply only the ones the repo's types imply, and read them in `WORKFLOW.md` 5A rather than from this list.

## 5B: Trace Scenarios

For each applicable scenario, evaluate every job's `if:`/`needs:` against the inputs and compare the predicted run/skip, version, release, and artifact end state to the expected table in `WORKFLOW.md` 5B. The load-bearing ones:

- **S1** a PR touching a target: that target smoke-builds, nothing uploads, the aggregator succeeds.
- **S5/S6** a bot push to `main`: publishes only when code-affecting, and a human push never does.
- **S7** a publish run builds the one trigger branch with the right classification and leaves no dangling artifacts.
- **S8** a dispatch from a ref other than `main`/`develop` fails fast.
- **S9** a no-op re-run: release-create skipped, registries dedupe, PyPI build artifact still deleted, Docker still re-pushes.
- **S10** branch and version classification disagree: the gate fails loud and everything downstream skips.
- **S12/S13** a deploy dispatch: ref gate first, environment re-asserted, pointer flip separate, live check names the release, and a production deploy from a non-default ref fails before anything is written.

## 5C: Live Probe

Only for what a static trace cannot settle: a trivial PR to confirm S1, a smoke push-probe of both branches' version classification, registry queries after a real publish, and the artifact lifecycle read from a real run's logs. The deploy ref gate is verified only by tripping it, and that dispatch is the maintainer's to run: the agent prepares the command and reads back the four evidence items (gate conclusion, its error text, every downstream job skipped, deployment count unchanged), and a harness refusal to fire it is the control working, never something to re-shape.

## Verdict

Operational iff every applicable 5A item passes and every applicable 5B scenario matches, with the failing guarantees and their triggering inputs named, and the N/A list recorded. Per-project-type walkthroughs mapping scenarios onto targets, including source-only, static-site, and operational shapes, are `WORKFLOW.md` section 6.
