# Homelab Runner Selection Plan

This document tracks the design and rollout for [issue #889][issue-889]. It is a consultation artifact, not an approved implementation contract.

## Table of Contents <!-- omit from toc -->

- [Status](#status)
- [Decision Outcome](#decision-outcome)
- [Requirements Before Resuming](#requirements-before-resuming)
- [Objective](#objective)
- [Non-Goals](#non-goals)
- [Primary Constraint: Copilot Code Review](#primary-constraint-copilot-code-review)
- [Configuration Contract](#configuration-contract)
- [Selection Architecture](#selection-architecture)
- [Trust Contract](#trust-contract)
- [Public Repository Boundary](#public-repository-boundary)
- [First Canary](#first-canary)
- [Copilot Acceptance Evidence](#copilot-acceptance-evidence)
- [Structural Verification](#structural-verification)
- [Planned Repository Surfaces](#planned-repository-surfaces)
- [Risks and Controls](#risks-and-controls)
- [Rollback](#rollback)
- [Consultation Decisions](#consultation-decisions)
- [Implementation Checkpoints](#implementation-checkpoints)

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

## Requirements Before Resuming

Implementation remains blocked until the maintainer approves the consultation decisions in this document.

Before implementation resumes, verify the current GitHub runner controls against GitHub's documentation and organization settings. The design depends on organization-level Copilot controls and runner-group workflow restrictions that can change independently of this repository.

The implementation must establish infrastructure authorization before it enables homelab selection. A selector, labels, actor checks, and static workflow tests are routing controls. They do not prevent another workflow from requesting the same labels directly.

## Objective

One workflow definition selects either GitHub-hosted Ubuntu or a trusted homelab runner. Missing or invalid configuration selects GitHub-hosted Ubuntu.

The design does not attempt an automatic availability fallback. GitHub chooses one target when it queues a job.

The first canary proves ordinary hub CI on a homelab runner in a restricted organization runner group. Copilot code review remains on GitHub-hosted infrastructure.

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

The organization-level Copilot runner type selects a standard GitHub-hosted runner. Repository customization of the Copilot runner type is disabled.

The proposed implementation adds a dedicated `.github/workflows/copilot-code-review.yml`. It pins `copilot-setup-steps` to `ubuntu-24.04` and begins with a step that fails unless `runner.environment` is `github-hosted`.

The organization policy is the authorization boundary. The workflow pin and runtime assertion detect configuration drift. The assertion does not make a job safe after GitHub has assigned it to the wrong runner.

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

The selector validates the requested target, event, actor, and ref. It emits one JSON `runs-on` value.

GitHub-hosted output:

```json
["ubuntu-24.04"]
```

Homelab output:

```json
{"group":"homelab","labels":["self-hosted","linux","x64","homelab","ubuntu-24.04"]}
```

Dependent jobs pass the output through `fromJSON` in `runs-on`. No caller copies the authorization expression.

The selector job adds a small GitHub-hosted job to each validation run. This cost buys one auditable routing decision for the fleet.

[GitHub treats runner labels as cumulative requirements][github-runner-selection]. The homelab object therefore selects only a runner in the restricted group carrying every listed label.

## Trust Contract

The selector permits homelab execution only when every applicable condition passes.

Allowed actors are exactly:

- `ptr727`
- `ptr727-codegen[bot]`

Dependabot-triggered work always selects GitHub-hosted Ubuntu. A dependency update can introduce untrusted executable code even when its pull request originates in this repository.

Homelab events are exactly:

- `push`
- `workflow_dispatch`

The homelab ref must equal the protected branch named by the runner group's selected-workflow restriction. Pull requests, schedules, and every other event select GitHub-hosted Ubuntu.

An unknown event always selects GitHub-hosted Ubuntu. In particular, `pull_request_target` receives no implicit homelab authorization.

Every rejected homelab request emits a visible warning. The warning names the failed policy check without exposing sensitive values.

Runner labels provide routing, not authorization. Existing workflow trigger, permission, environment, and actor checks remain in force.

The homelab runner belongs to a dedicated organization runner group. The group allows this repository and the approved validation workflow only. Its selected-workflow restriction names the protected workflow ref. The job selects both the group and its labels. Direct label requests from any other workflow cannot reach the runner.

## Public Repository Boundary

The first runner is registered at the organization scope so runner-group restrictions can enforce repository and workflow access. It is not available organization-wide.

The CloudInit implementation retains its private-repository default. Public-repository support requires a separate, explicit opt-in.

The proposed provisioning contract requires:

- An explicit public-repository opt-in with a false default.
- `allows_public_repositories` equals the explicit opt-in and is false otherwise.
- An exact repository owner and name.
- A dedicated organization runner group restricted to `ptr727/ProjectTemplate`.
- A selected-workflow restriction for the approved validation workflow at its protected ref.
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
3. Provision the homelab runner in its restricted organization runner group.
4. Confirm the runner carries every required label.
5. Set `FLEET_RUNNER_TARGET` to `homelab`.
6. Dispatch the approved validation workflow from its protected ref as `ptr727`.
7. Confirm the canary job selects the restricted group and runs on the homelab runner.
8. Confirm Copilot's agentic job runs on GitHub-hosted Ubuntu.
9. Exercise the external pull request path.
10. Confirm the external path selects GitHub-hosted Ubuntu.
11. Take the homelab runner offline for a maximum of ten minutes.
12. Confirm the trusted job remains visibly queued without fallback.
13. Cancel the queued workflow when the observation bound expires.
14. Confirm the cancellation reaches a terminal state.
15. Restore the runner and finish the canary with a new workflow run.

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
- A Dependabot pull request selects GitHub-hosted Ubuntu.
- An untrusted actor.
- Internal pull request selects GitHub-hosted Ubuntu.
- Fork pull request selects GitHub-hosted Ubuntu.
- Push event.
- Manual dispatch event.
- Scheduled event selects GitHub-hosted Ubuntu.
- Unsupported event.
- Exact GitHub-hosted label output.
- Exact homelab group-and-label output.
- A direct-label workflow cannot use the homelab runner group.
- An unapproved workflow ref cannot use the homelab runner group.
- Public-repository access follows the explicit opt-in exactly.
- A fixed GitHub-hosted Copilot runner.
- Absence of the fleet variable from Copilot configuration.
- Absence of homelab labels from Copilot configuration.

Static workflow verification also covers action pinning, actionlint custom labels, job dependencies, and workflow syntax.

Live verification covers both selection branches. Only a live run can prove runner registration and Copilot integration.
Before the runner-group checkpoint is complete, capture live API evidence for the effective repository allowlist, `restricted_to_workflows: true`, the exact selected-workflow path and protected ref, and `allows_public_repositories`. Assert the restriction flag and selected-workflow value together because GitHub ignores the value when the flag is false. Run negative canaries that prove direct-label and unapproved-ref requests cannot reach the group. Link provisioning evidence from the owning CloudInit repository rather than inferring it from this plan.

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
| Pull request code reaches a persistent runner | Every pull request selects GitHub-hosted execution, and runner-group restrictions reject unapproved workflows |
| An unexpected actor reaches homelab | Exact actor allowlist fails closed |
| A dependency update executes on a persistent runner | Dependabot always selects GitHub-hosted Ubuntu |
| A workflow bypasses the selector with direct labels | Runner-group selected-workflow restrictions reject it |
| A new event bypasses assumptions | Exact event allowlist fails closed |
| Invalid configuration widens access | Unknown values select GitHub-hosted Ubuntu |
| An offline runner stalls CI | Queued state is documented and observed during canary |
| Copilot is routed to a persistent VM | Organization policy requires standard GitHub-hosted runners and disables repository overrides |
| Copilot review silently loses agentic context | Acceptance requires review-session evidence |
| A public runner becomes fleet-wide | Its organization runner group allows only the named repository and workflow |
| Provisioning weakens the private default | Public support requires an explicit false-by-default opt-in |

## Rollback

Set `FLEET_RUNNER_TARGET` to `github-hosted` at its authoritative repository scope. Do not unset it, because an inherited value could become effective. No workflow source rollback is required for this operational response.

Cancel every queued or in-progress homelab workflow. Confirm each cancellation reaches a terminal state before restoring service.

Disable or remove the runner from its restricted group if routing remains unexpected. Preserve its diagnostic logs before removal.

The Copilot workflow remains GitHub-hosted throughout rollback.

## Consultation Decisions

Implementation waits for an explicit decision on each item.

- [ ] Use `FLEET_RUNNER_TARGET` as the configuration variable.
- [ ] Accept only `github-hosted` and `homelab` in phase 1.
- [ ] Use a GitHub-hosted selector job as the single routing decision point.
- [ ] Use a restricted organization runner group as the infrastructure authorization point.
- [ ] Select the homelab runner by both group and labels.
- [ ] Route the reusable validation lint job as the first canary.
- [ ] Decide whether unit-test and repository-validation join the first canary.
- [ ] Add a dedicated GitHub-hosted `copilot-code-review.yml`.
- [ ] Select standard GitHub-hosted Copilot runners at the organization level and disable repository overrides.
- [ ] Keep standalone self-hosted Copilot review out of phase 1.
- [ ] Reserve ARC evaluation for phase 2.
- [ ] Restrict the organization runner group to the public hub and approved workflow ref.
- [ ] Run the first homelab canary by dispatch from the protected workflow ref.
- [ ] Add a false-by-default public-repository opt-in to CloudInit.
- [ ] Approve the trusted actor and event allowlists, with Dependabot excluded.
- [ ] Approve the offline-runner observation test.

## Implementation Checkpoints

- [x] Phase 1 postponed after return-on-investment review.
- [ ] Consultation decisions approved.
- [ ] ProjectTemplate implementation reviewed.
- [ ] Structural tests pass.
- [ ] GitHub-hosted default branch exercised.
- [ ] CloudInit public opt-in reviewed in its owning repository.
- [ ] Restricted organization runner group and runner registered.
- [ ] Live runner-group API evidence records the repository allowlist, `restricted_to_workflows: true`, exact selected-workflow path and protected ref, and public-repository setting.
- [ ] Direct-label and unapproved-ref negative canaries are rejected.
- [ ] CloudInit provisioning evidence is linked from its owning repository.
- [ ] Protected-ref homelab canary succeeds.
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
