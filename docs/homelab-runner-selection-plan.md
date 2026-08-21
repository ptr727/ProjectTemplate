# Homelab Runner Selection Plan

This document tracks the design and rollout for [issue #889][issue-889]. It is a consultation artifact, not an approved implementation contract.

## Status

**State:** Postponed\
**Implementation:** Not started\
**First workload:** `ptr727/ProjectTemplate` validation\
**Phase boundary:** Explicit selection and one persistent-runner canary

The plan requires a new maintainer decision before implementation starts.

## Decision Outcome

The maintainer postpones implementation after reviewing the expected return on investment.

Actions minutes savings do not justify the added runner, security, and operating complexity. Supported self-hosted Copilot review would also require an ARC deployment.

Ordinary CI savings alone do not justify separating CI onto a persistent runner while Copilot remains GitHub-hosted. This document preserves the design for reconsideration.

## Objective

One workflow definition selects either GitHub-hosted Ubuntu or a trusted homelab runner. Missing or invalid configuration selects GitHub-hosted Ubuntu.

The design does not attempt an automatic availability fallback. GitHub chooses one target when it queues a job.

The first canary proves ordinary hub CI on a repository-scoped homelab runner. Copilot code review remains on GitHub-hosted infrastructure.

## Non-Goals

This phase does not provide:

- Automatic virtual machine creation.
- Ephemeral self-hosted runners.
- Fixed-pool reconciliation.
- Actions Runner Controller deployment.
- Self-hosted Copilot code review.
- Automatic fallback when the homelab runner is offline.

Those items belong to the phase 2 runner-lifecycle design.

## Primary Constraint: Copilot Code Review

Copilot code review uses GitHub Actions for agentic context gathering. [GitHub-hosted runners are its default execution environment][github-copilot-runners].

GitHub supports self-hosted Copilot code review only through ARC-managed scale sets. GitHub warns against standalone non-ARC runners for this workload.

Ordinary fleet CI and Copilot code review therefore have separate runner policies:

| Workload | Phase 1 Runner Policy |
| --- | --- |
| Fleet validation jobs | Explicitly select GitHub-hosted or homelab |
| Fleet selector job | Always GitHub-hosted |
| Copilot code review | Always GitHub-hosted |
| Copilot cloud agent | Unchanged |

The fleet runner variable never reaches Copilot configuration. Homelab labels never appear in the Copilot workflow.

The proposed implementation adds a dedicated `.github/workflows/copilot-code-review.yml`. It pins `copilot-setup-steps` to `ubuntu-24.04`.

That file prevents a future `copilot-setup-steps.yml` from unintentionally changing the code-review runner. GitHub gives the dedicated code-review file precedence.

## Configuration Contract

The proposed repository configuration variable is `FLEET_RUNNER_TARGET`.

It accepts two values:

| Configured Value | Effective Target |
| --- | --- |
| Missing | `github-hosted` |
| `github-hosted` | `github-hosted` |
| `homelab`, trusted invocation | `homelab` |
| `homelab`, untrusted invocation | `github-hosted` with a warning |
| Any other value | `github-hosted` with a warning |

The explicit target leaves room for future values such as `arc`. A boolean would not express that extension cleanly.

## Selection Architecture

A small selector job runs on `ubuntu-24.04` before any selectable validation job.

The selector validates the requested target, event, actor, and pull request origin. It emits one JSON runner-label array.

GitHub-hosted output:

```json
["ubuntu-24.04"]
```

Homelab output:

```json
["self-hosted","linux","x64","homelab","ubuntu-24.04"]
```

Dependent jobs pass the output through `fromJSON` in `runs-on`. No caller copies the authorization expression.

The selector job adds a small GitHub-hosted job to each validation run. This cost buys one auditable security decision for the fleet.

[GitHub treats runner labels as cumulative requirements][github-runner-selection]. The homelab array therefore selects only a runner carrying every listed label.

## Trust Contract

The selector permits homelab execution only when every applicable condition passes.

Allowed actors are exactly:

- `ptr727`
- `dependabot[bot]`
- `ptr727-codegen[bot]`

Allowed events are exactly:

- `push`
- `pull_request`
- `workflow_dispatch`
- `schedule`

A pull request also requires its head repository to equal the workflow repository. A fork pull request always selects GitHub-hosted Ubuntu.

An unknown event always selects GitHub-hosted Ubuntu. In particular, `pull_request_target` receives no implicit homelab authorization.

Every rejected homelab request emits a visible warning. The warning names the failed policy check without exposing sensitive values.

Runner labels provide routing, not authorization. Existing workflow trigger, permission, environment, and actor checks remain in force.

## Public Repository Boundary

The first runner is scoped specifically to `ptr727/ProjectTemplate`. It is not an organization-wide runner.

The CloudInit implementation retains its private-repository default. Public-repository support requires a separate, explicit opt-in.

The proposed provisioning contract requires:

- An explicit public-repository opt-in with a false default.
- An exact repository owner and name.
- Repository-scoped runner registration.
- The dedicated `homelab` label.
- A visible provisioning warning for a public repository.
- Documentation that workflow authorization remains mandatory.

ProjectTemplate does not contain the CloudInit implementation. Its owning repository carries that change and its tests.

## First Canary

The reusable validation workflow is the first workload. Its lint job is the most useful initial homelab exercise.

The lint job covers checkout, Docker, shell tools, network access, and the repository's actual merge gate. No-op jobs provide weaker evidence.

The rollout proceeds in these stages:

1. Add the selector while `FLEET_RUNNER_TARGET` is absent.
2. Confirm every selectable job remains GitHub-hosted.
3. Provision the repository-scoped homelab runner.
4. Confirm the runner carries every required label.
5. Set `FLEET_RUNNER_TARGET` to `homelab`.
6. Open a same-repository pull request as `ptr727`.
7. Confirm the canary job runs on the homelab runner.
8. Confirm Copilot's agentic job runs on GitHub-hosted Ubuntu.
9. Exercise the external pull request path.
10. Confirm the external path selects GitHub-hosted Ubuntu.
11. Take the homelab runner offline for a bounded observation.
12. Confirm the trusted job remains visibly queued without fallback.
13. Restore the runner and finish the canary.

The rollout stops after any unexpected target selection. It also stops if Copilot delivers only a reduced review.

## Copilot Acceptance Evidence

A Copilot review comment alone is insufficient. GitHub can produce a reduced review when its agentic Actions work fails.

The canary records all of this evidence:

- Copilot completes a pull request review.
- The review session shows agentic context gathering.
- The associated Copilot Actions work completes.
- Copilot runs on GitHub-hosted Ubuntu.
- Ordinary validation runs on the homelab runner.
- Copilot review logs remain available after validation completes.
- The review includes the repository instructions and available review skill context.

The evidence should include links to the pull request, validation run, Copilot review, and review session.

## Structural Verification

Automated tests cover:

- Missing target.
- Explicit GitHub-hosted target.
- Invalid target.
- Every trusted actor.
- An untrusted actor.
- Internal pull request.
- Fork pull request.
- Push event.
- Manual dispatch event.
- Scheduled event.
- Unsupported event.
- Exact GitHub-hosted label output.
- Exact homelab label output.
- A fixed GitHub-hosted Copilot runner.
- Absence of the fleet variable from Copilot configuration.
- Absence of homelab labels from Copilot configuration.

Static workflow verification also covers action pinning, actionlint custom labels, job dependencies, and workflow syntax.

Live verification covers both selection branches. Only a live run can prove runner registration and Copilot integration.

## Planned Repository Surfaces

Implementation is expected to touch these ProjectTemplate surfaces:

- A canonical runner-selector action and its tests.
- `.github/workflows/validate-task.yml` for the first selectable jobs.
- `.github/workflows/copilot-code-review.yml` for the explicit Copilot boundary.
- `.github/actionlint.yaml` for custom homelab labels.
- `WORKFLOW.md` for the behavioral and security contract.
- `docs/reusable-workflows.md` for selector ownership and caller behavior.
- Applicable spec and audit checks for structural enforcement.
- This plan with decisions and rollout evidence.

The CloudInit repository carries its public opt-in, provisioning tests, and operator documentation.

## Risks and Controls

| Risk | Control |
| --- | --- |
| Fork code reaches a persistent runner | Fork identity check forces GitHub-hosted execution |
| An unexpected actor reaches homelab | Exact actor allowlist fails closed |
| A new event bypasses assumptions | Exact event allowlist fails closed |
| Invalid configuration widens access | Unknown values select GitHub-hosted Ubuntu |
| An offline runner stalls CI | Queued state is documented and observed during canary |
| Copilot is routed to a persistent VM | Dedicated Copilot workflow pins GitHub-hosted Ubuntu |
| Copilot review silently loses agentic context | Acceptance requires review-session evidence |
| A public runner becomes fleet-wide | Repository-scoped registration is mandatory |
| Provisioning weakens the private default | Public support requires an explicit false-by-default opt-in |

## Rollback

Unset `FLEET_RUNNER_TARGET` to return selectable jobs to GitHub-hosted Ubuntu. No workflow source rollback is required for this operational response.

Disable or remove the repository-scoped runner registration if routing remains unexpected. Preserve its diagnostic logs before removal.

The Copilot workflow remains GitHub-hosted throughout rollback.

## Consultation Decisions

Implementation waits for an explicit decision on each item.

- [ ] Use `FLEET_RUNNER_TARGET` as the configuration variable.
- [ ] Accept only `github-hosted` and `homelab` in phase 1.
- [ ] Use a GitHub-hosted selector job as the single authorization point.
- [ ] Route the reusable validation lint job as the first canary.
- [ ] Decide whether unit-test and repository-validation join the first canary.
- [ ] Add a dedicated GitHub-hosted `copilot-code-review.yml`.
- [ ] Keep standalone self-hosted Copilot review out of phase 1.
- [ ] Reserve ARC evaluation for phase 2.
- [ ] Require repository-scoped registration for the public hub.
- [ ] Add a false-by-default public-repository opt-in to CloudInit.
- [ ] Approve the trusted actor and event allowlists.
- [ ] Approve the offline-runner observation test.

## Implementation Checkpoints

- [x] Phase 1 postponed after return-on-investment review.
- [ ] Consultation decisions approved.
- [ ] ProjectTemplate implementation reviewed.
- [ ] Structural tests pass.
- [ ] GitHub-hosted default branch exercised.
- [ ] CloudInit public opt-in reviewed in its owning repository.
- [ ] Repository-scoped runner registered.
- [ ] Trusted homelab canary succeeds.
- [ ] Fork or external path stays GitHub-hosted.
- [ ] Copilot agentic review succeeds on GitHub-hosted infrastructure.
- [ ] Offline queued behavior is observed and documented.
- [ ] Rollout evidence is linked from this document.
- [ ] Phase 1 is accepted.

<!-- GitHub -->

[issue-889]: https://github.com/ptr727/ProjectTemplate/issues/889

<!-- External -->

[github-copilot-runners]: https://docs.github.com/en/copilot/how-tos/copilot-on-github/set-up-copilot/configure-runners
[github-runner-selection]: https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job
