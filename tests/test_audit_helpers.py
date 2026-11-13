#!/usr/bin/env python3
"""spec/audit.py's helpers on constructed fixtures, with no network read."""

from __future__ import annotations

import base64
import contextlib
import inspect
import io
import json
import locale
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "spec"))
import audit
import audit_markdown
from git_env import git_location_cleared

_LOCATION = contextlib.ExitStack()
IS_WINDOWS = sys.platform == "win32"


def setUpModule() -> None:
    # A git hook sets GIT_DIR, which would otherwise place every temporary repository inside the hook's own.
    _LOCATION.enter_context(git_location_cleared())


def tearDownModule() -> None:
    _LOCATION.close()


PR_CHECK_NAME = "Check pull request workflow status job"
# The name WORKFLOW.md D1.5 gives a workflow that another event also triggers, the ruleset-bound one only on a pull_request run.
PR_ONLY_EXPRESSION = (
    "${{ github.event_name == 'pull_request' && '"
    + PR_CHECK_NAME
    + "' || 'Check workflow status job' }}"
)
PR_CHECK = "  check-workflow-status:\n    name: Check pull request workflow status job\n    needs: [changes]\n    runs-on: ubuntu-latest\n"
PR_HEAD = "name: Test\non: pull_request\njobs:\n  changes:\n    runs-on: ubuntu-latest\n"
PR_CONTRACT = {
    "requiredJobKeys": ["check-workflow-status"],
    "requiredCheckName": "Check pull request workflow status job",
}
# The validate-task.yml stub contract: a caller's validate job must exist and reach the hub task by name.
# A token check alone would pass a stub that drops the validate job entirely, since it only runs for a job that is present.
# Naming the validate job in requiredJobKeys too catches a dropped job on its own.
PR_STUB_CONTRACT = dict(
    PR_CONTRACT,
    requiredJobKeys=["check-workflow-status", "validate"],
    requireTokensInJob={"validate": ["validate-task.yml"]},
)
PR_VALIDATE_HEAD = (
    "name: Test\non: pull_request\njobs:\n"
    "  validate:\n"
    "    name: Validate sources job\n"
    "    uses: ptr727/ProjectTemplate/.github/workflows/validate-task.yml@"
    + "a" * 40
    + " # 2.0.1\n"
)
PR_VALIDATE_INLINE = (
    "name: Test\non: pull_request\njobs:\n"
    "  validate:\n"
    "    name: Validate sources job\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n      - run: echo inline lint\n"
)
# A validate-task.yml stub's own aggregator needs the validate job, never the release-shape changes job.
# The shared PR_CHECK fixture needs a changes job these two fixtures do not define, so this one needs validate instead.
PR_CHECK_VALIDATE = "  check-workflow-status:\n    name: Check pull request workflow status job\n    needs: [validate]\n    runs-on: ubuntu-latest\n"
GH_REL = (
    "  github-release:\n    needs: [get-version, build-widget]\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - uses: actions/download-artifact@v4\n        with:\n          pattern: release-asset-${{ inputs.branch }}-*\n          merge-multiple: true\n"
)
REL_HEAD = (
    "name: Build Release\non:\n  workflow_call:\njobs:\n"
    "  get-version:\n    runs-on: ubuntu-latest\n    steps: []\n"
    "  build-widget:\n    runs-on: ubuntu-latest\n    steps: []\n"
)
REL_OK = REL_HEAD + GH_REL
REL_CONTRACT = {
    "requiredJobKeys": ["get-version", "github-release"],
    "artifactNameToken": "release-asset-",
    "requireTokensInJob": {"github-release": ["pattern:", "merge-multiple:"]},
    "forbidTokensInJob": {"github-release": ["artifact-ids:"]},
}
# The merge-bot caller stub, the shape every repo carries once the merge-bot is hub-hosted.
BOT_STUB = (
    "jobs:\n"
    "  merge-bot:\n"
    "    name: Merge bot pull request job\n"
    "    uses: acme/hub/.github/workflows/merge-bot-task.yml@" + "a" * 40 + " # 2.0.1\n"
    "    secrets:\n"
    "      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}\n"
    "      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}\n"
)
BOT_CONTRACT = {
    "requiredJobKeys": ["merge-bot"],
    "requireTokensInJob": {
        "merge-bot": ["merge-bot-task.yml", "CODEGEN_APP_CLIENT_ID", "CODEGEN_APP_PRIVATE_KEY"]
    },
}
# The publish-release.yml caller stub once the release chain is hub-hosted, plan, validate and publish job keys.
# Each names its hub task by token, per docs/reusable-workflows.md "Adopting the Release Chain".
# Validate's own token requirement is what stops a stub from satisfying the job key with a no-op job.
PUBLISH_STUB = (
    "jobs:\n"
    "  plan:\n"
    "    name: Plan release job\n"
    "    uses: acme/hub/.github/workflows/publish-plan-task.yml@" + "a" * 40 + " # 2.0.1\n"
    "    with:\n"
    "      event_name: ${{ github.event_name }}\n"
    "  validate:\n"
    "    name: Validate sources job\n"
    "    needs: [plan]\n"
    "    if: ${{ needs.plan.outputs.publish == 'true' }}\n"
    "    uses: ./.github/workflows/validate-task.yml\n"
    "  publish:\n"
    "    name: Publish project release job\n"
    "    needs: [plan, validate]\n"
    "    if: ${{ needs.plan.outputs.publish == 'true' && needs.validate.result == 'success' }}\n"
    "    uses: acme/hub/.github/workflows/build-release-task.yml@" + "a" * 40 + " # 2.0.1\n"
    "    with:\n"
    "      github: true\n"
)
PUBLISH_CONTRACT = {
    "requiredJobKeys": ["plan", "validate", "publish"],
    "requireTokensInJob": {
        "plan": ["publish-plan-task.yml"],
        "validate": ["validate-task.yml"],
        "publish": ["build-release-task.yml", "needs.validate.result == 'success'"],
    },
}
INTERFACE_CASES = [
    ("conformant PR workflow", PR_HEAD + PR_CHECK, PR_CONTRACT, 0),
    ("PR workflow missing the required job and its check name", PR_HEAD, PR_CONTRACT, 2),
    (
        "PR workflow with a renamed check",
        PR_HEAD + PR_CHECK.replace("Check pull request workflow status job", "Renamed"),
        PR_CONTRACT,
        1,
    ),
    (
        "check name present only in a run step, not as a job name",
        PR_HEAD
        + "  check-workflow-status:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo Check pull request workflow status job\n",
        PR_CONTRACT,
        1,
    ),
    (
        "check name present only as a step name, not the job name",
        PR_HEAD
        + "  check-workflow-status:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Check pull request workflow status job\n        run: true\n",
        PR_CONTRACT,
        1,
    ),
    (
        "a pull_request-only expression name on a workflow a dispatch also triggers",
        PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
        + PR_CHECK.replace(PR_CHECK_NAME, PR_ONLY_EXPRESSION),
        PR_CONTRACT,
        0,
    ),
    (
        "a literal check name on a workflow a dispatch also triggers",
        PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
        + PR_CHECK,
        PR_CONTRACT,
        1,
    ),
    (
        "a literal check name on a workflow a push also triggers, in the list form",
        PR_HEAD.replace("on: pull_request\n", "on: [pull_request, push]\n") + PR_CHECK,
        PR_CONTRACT,
        1,
    ),
    (
        "a literal check name beside a gated one, on a workflow a dispatch also triggers",
        PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
        + PR_CHECK.replace(PR_CHECK_NAME, PR_ONLY_EXPRESSION)
        + PR_CHECK.replace("check-workflow-status:", "extra:"),
        PR_CONTRACT,
        1,
    ),
    (
        "two literal check names on a workflow a dispatch also triggers, one finding each",
        PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
        + PR_CHECK
        + PR_CHECK.replace("check-workflow-status:", "extra:"),
        PR_CONTRACT,
        2,
    ),
    (
        "an expression falling back to the check name beside a gated one",
        PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
        + PR_CHECK.replace(PR_CHECK_NAME, PR_ONLY_EXPRESSION)
        + PR_CHECK.replace("check-workflow-status:", "extra:").replace(
            PR_CHECK_NAME,
            "${{ github.event_name == 'pull_request' && 'Other job' || '" + PR_CHECK_NAME + "' }}",
        ),
        PR_CONTRACT,
        1,
    ),
    (
        "an expression falling back to the check name with no gated job, both findings",
        PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
        + PR_CHECK.replace(
            PR_CHECK_NAME,
            "${{ github.event_name == 'pull_request' && 'Other job' || '" + PR_CHECK_NAME + "' }}",
        ),
        PR_CONTRACT,
        2,
    ),
    (
        "an expression falling back to the check name beside one naming another job on every event",
        PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
        + PR_CHECK.replace(
            PR_CHECK_NAME,
            "${{ github.event_name == 'pull_request' && 'Other job' || '" + PR_CHECK_NAME + "' }}",
        )
        + PR_CHECK.replace("check-workflow-status:", "extra:").replace(
            PR_CHECK_NAME,
            "${{ github.event_name == 'pull_request' && 'Other job' || 'Other job' }}",
        ),
        PR_CONTRACT,
        2,
    ),
    (
        "an expression yielding the check name on every event, on a workflow a dispatch also triggers",
        PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
        + PR_CHECK.replace(
            PR_CHECK_NAME,
            "${{ github.event_name == 'pull_request' && '"
            + PR_CHECK_NAME
            + "' || '"
            + PR_CHECK_NAME
            + "' }}",
        ),
        PR_CONTRACT,
        1,
    ),
    (
        "an expression yielding the check name on every event",
        PR_HEAD
        + PR_CHECK.replace(
            PR_CHECK_NAME,
            "${{ github.event_name == 'pull_request' && '"
            + PR_CHECK_NAME
            + "' || '"
            + PR_CHECK_NAME
            + "' }}",
        ),
        PR_CONTRACT,
        1,
    ),
    (
        "an expression yielding the check name only on a push",
        PR_HEAD
        + PR_CHECK.replace(PR_CHECK_NAME, PR_ONLY_EXPRESSION.replace("'pull_request'", "'push'")),
        PR_CONTRACT,
        1,
    ),
    (
        "PR stub validate job reaching the hub validate-task",
        PR_VALIDATE_HEAD + PR_CHECK_VALIDATE,
        PR_STUB_CONTRACT,
        0,
    ),
    (
        "PR stub validate job still carrying an inline lint job",
        PR_VALIDATE_INLINE + PR_CHECK_VALIDATE,
        PR_STUB_CONTRACT,
        1,
    ),
    (
        "PR stub dropping the validate job entirely",
        PR_HEAD + PR_CHECK,
        PR_STUB_CONTRACT,
        1,
    ),
    ("conformant release task", REL_OK, REL_CONTRACT, 0),
    (
        "release task with an artifact-ids fork in github-release",
        REL_OK.replace(
            "          merge-multiple: true\n",
            "          merge-multiple: true\n          artifact-ids: 123\n",
        ),
        REL_CONTRACT,
        1,
    ),
    (
        "release task missing merge-multiple in github-release",
        REL_OK.replace("          merge-multiple: true\n", ""),
        REL_CONTRACT,
        1,
    ),
    (
        "release task with an owned extra leaf job",
        REL_HEAD + "  build-extra:\n    runs-on: ubuntu-latest\n    steps: []\n" + GH_REL,
        REL_CONTRACT,
        0,
    ),
    (
        "absent job reports once, no redundant token findings",
        REL_HEAD,
        {
            "requiredJobKeys": ["github-release"],
            "requireTokensInJob": {"github-release": ["pattern:", "merge-multiple:"]},
        },
        1,
    ),
    (
        "a forbidden token only in a comment is ignored",
        REL_OK.replace(
            "          merge-multiple: true\n",
            "          merge-multiple: true\n          # never an artifact-ids: fork here\n",
        ),
        REL_CONTRACT,
        0,
    ),
    (
        "a required token only in a comment does not count",
        REL_OK.replace(
            "          merge-multiple: true\n", "          # merge-multiple: true (was here)\n"
        ),
        REL_CONTRACT,
        1,
    ),
    (
        "merge-bot caller stub reaching the hub task with both secrets mapped",
        BOT_STUB,
        BOT_CONTRACT,
        0,
    ),
    (
        "merge-bot caller stub with the hub's own local uses",
        BOT_STUB.replace(
            "acme/hub/.github/workflows/merge-bot-task.yml@" + "a" * 40 + " # 2.0.1",
            "./.github/workflows/merge-bot-task.yml",
        ),
        BOT_CONTRACT,
        0,
    ),
    (
        "merge-bot copy still carrying the job bodies reports the missing caller job once",
        (
            "jobs:\n  merge-dependabot:\n    runs-on: ubuntu-latest\n    steps: []\n"
            "  disable-auto-merge-on-maintainer-push:\n    runs-on: ubuntu-latest\n    steps: []\n"
        ),
        BOT_CONTRACT,
        1,
    ),
    (
        "merge-bot caller stub that maps only one secret",
        BOT_STUB.replace(
            "      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}\n", ""
        ),
        BOT_CONTRACT,
        1,
    ),
    (
        "publish-release.yml stub reaching both hub tasks by pin",
        PUBLISH_STUB,
        PUBLISH_CONTRACT,
        0,
    ),
    (
        "publish-release.yml stub with the hub's own local uses",
        PUBLISH_STUB.replace(
            "acme/hub/.github/workflows/publish-plan-task.yml@" + "a" * 40 + " # 2.0.1",
            "./.github/workflows/publish-plan-task.yml",
        ).replace(
            "acme/hub/.github/workflows/build-release-task.yml@" + "a" * 40 + " # 2.0.1",
            "./.github/workflows/build-release-task.yml",
        ),
        PUBLISH_CONTRACT,
        0,
    ),
    (
        "publish-release.yml stub missing the plan job reports it once",
        "jobs:\n"
        "  validate:\n"
        "    name: Validate sources job\n"
        "    uses: ./.github/workflows/validate-task.yml\n"
        "  publish:\n"
        "    name: Publish project release job\n"
        "    needs: [validate]\n"
        "    if: ${{ needs.validate.result == 'success' }}\n"
        "    uses: acme/hub/.github/workflows/build-release-task.yml@" + "a" * 40 + " # 2.0.1\n",
        PUBLISH_CONTRACT,
        1,
    ),
    (
        "publish-release.yml stub missing the validate job reports it once",
        "jobs:\n"
        "  plan:\n"
        "    name: Plan release job\n"
        "    uses: acme/hub/.github/workflows/publish-plan-task.yml@" + "a" * 40 + " # 2.0.1\n"
        "  publish:\n"
        "    name: Publish project release job\n"
        "    needs: [plan, validate]\n"
        "    if: ${{ needs.validate.result == 'success' }}\n"
        "    uses: acme/hub/.github/workflows/build-release-task.yml@" + "a" * 40 + " # 2.0.1\n",
        PUBLISH_CONTRACT,
        1,
    ),
    (
        "publish-release.yml stub whose publish job never names build-release-task.yml",
        PUBLISH_STUB.replace(
            "acme/hub/.github/workflows/build-release-task.yml@" + "a" * 40 + " # 2.0.1",
            "./.github/workflows/build-release-task-renamed.yml",
        ),
        PUBLISH_CONTRACT,
        1,
    ),
    (
        "publish-release.yml stub whose validate job never names validate-task.yml",
        PUBLISH_STUB.replace(
            "    uses: ./.github/workflows/validate-task.yml\n",
            "    uses: ./.github/workflows/validate-task-renamed.yml\n",
        ),
        PUBLISH_CONTRACT,
        1,
    ),
    (
        "publish-release.yml stub carrying its publish condition in a folded if: >-",
        PUBLISH_STUB.replace(
            "    if: ${{ needs.plan.outputs.publish == 'true' && needs.validate.result == 'success' }}\n",
            "    if: >-\n"
            "      ${{ needs.plan.outputs.publish == 'true'\n"
            "      && needs.validate.result == 'success' }}\n",
        ),
        PUBLISH_CONTRACT,
        0,
    ),
    (
        "publish-release.yml stub whose folded if: >- wraps inside the required token",
        PUBLISH_STUB.replace(
            "    if: ${{ needs.plan.outputs.publish == 'true' && needs.validate.result == 'success' }}\n",
            "    if: >-\n"
            "      ${{ needs.plan.outputs.publish == 'true' && needs.validate.result\n"
            "      == 'success' }}\n",
        ),
        PUBLISH_CONTRACT,
        0,
    ),
    (
        "publish-release.yml stub naming the validation token only in a name: | body",
        PUBLISH_STUB.replace(
            "    name: Publish project release job\n",
            "    name: |\n      needs.validate.result == 'success'\n",
        ).replace(" && needs.validate.result == 'success'", ""),
        PUBLISH_CONTRACT,
        1,
    ),
    (
        "publish-release.yml stub whose publish job ignores failed validation",
        PUBLISH_STUB.replace(" && needs.validate.result == 'success'", ""),
        PUBLISH_CONTRACT,
        1,
    ),
]
# The deploy-site.yml caller stub once deploy-site-task.yml is hub-hosted: no secrets: inherit, the required crossing secret named instead.
# GitHub documents secrets: inherit for same-org or enterprise callers, so the fleet does not use it across repositories.
# No job-level environment: on the caller, unsupported on a job with uses:.
DEPLOY_STUB = (
    "jobs:\n"
    "  assert-ref:\n    runs-on: ubuntu-latest\n    steps: []\n"
    "  validate:\n    uses: ./.github/workflows/validate-task.yml\n"
    "  deploy:\n"
    "    name: Deploy job\n"
    "    permissions:\n      contents: read\n"
    "    uses: acme/hub/.github/workflows/deploy-site-task.yml@" + "a" * 40 + " # 2.0.1\n"
    "    with:\n      environment: ${{ inputs.environment }}\n"
    "    secrets:\n      DEPLOY_SSH_PRIVATE_KEY: ${{ secrets.DEPLOY_SSH_PRIVATE_KEY }}\n"
)
DEPLOY_CONTRACT = {
    "requiredJobKeys": ["assert-ref", "validate", "deploy"],
    "requireTokensInJob": {
        "deploy": [
            "deploy-site-task.yml",
            # Anchored to the with: block specifically, not just 6-space indent.
            # A secret named "environment" would also land at that indent otherwise.
            # The task declares this input required, and a missing one fails at dispatch, not at audit time.
            "with:\n      environment:",
            "contents: read",
            "DEPLOY_SSH_PRIVATE_KEY",
        ]
    },
    "forbidTokensInJob": {
        # Indent-anchored to the job's own top level, the shape GitHub rejects on a job with uses:.
        "deploy": ["\n    environment:"]
    },
}
INTERFACE_CASES += [
    (
        "deploy-site.yml caller stub reaching the hub task with the crossing secret mapped",
        DEPLOY_STUB,
        DEPLOY_CONTRACT,
        0,
    ),
    (
        "deploy-site.yml caller stub still using secrets: inherit reports the missing secret",
        DEPLOY_STUB.replace(
            "    secrets:\n      DEPLOY_SSH_PRIVATE_KEY: ${{ secrets.DEPLOY_SSH_PRIVATE_KEY }}\n",
            "    secrets: inherit\n",
        ),
        DEPLOY_CONTRACT,
        1,
    ),
    (
        "deploy-site.yml caller stub reintroducing the invalid job-level environment: key",
        DEPLOY_STUB.replace(
            "    name: Deploy job\n",
            "    name: Deploy job\n    environment: ${{ inputs.environment }}\n",
        ),
        DEPLOY_CONTRACT,
        1,
    ),
    (
        "deploy-site.yml caller stub missing the required environment input",
        DEPLOY_STUB.replace("    with:\n      environment: ${{ inputs.environment }}\n", ""),
        DEPLOY_CONTRACT,
        1,
    ),
    (
        "deploy-site.yml caller stub with environment: under the wrong mapping still reports missing",
        # A same-indented environment: key under secrets: must not satisfy the with:-anchored requirement on its own.
        # The oddly-named secret's value is left generic so it does not incidentally satisfy the DEPLOY_SSH_PRIVATE_KEY token.
        DEPLOY_STUB.replace(
            "    with:\n      environment: ${{ inputs.environment }}\n",
            "    secrets:\n      environment: ${{ secrets.SOME_OTHER_SECRET }}\n",
        ),
        DEPLOY_CONTRACT,
        1,
    ),
    (
        "deploy-site.yml caller stub missing the contents: read grant the task's jobs declare",
        DEPLOY_STUB.replace("    permissions:\n      contents: read\n", ""),
        DEPLOY_CONTRACT,
        1,
    ),
    (
        # A block scalar crafted to contain the required token as string content must still report it missing.
        # The real `with:` mapping is removed here, so only the block scalar carries the text.
        "deploy-site.yml caller stub with the with:/environment: tokens only inside a block-scalar name still reports missing",
        DEPLOY_STUB.replace(
            "    name: Deploy job\n", "    name: |\n      with:\n      environment:\n"
        ).replace("    with:\n      environment: ${{ inputs.environment }}\n", ""),
        DEPLOY_CONTRACT,
        1,
    ),
]
# The stage-5 type-specific hub tasks carry no manifest entry yet, since no repo has adopted a caller stub for them.
# These fixtures exercise the contract adoption will register, proving the interface engine reads it correctly before any downstream repo depends on that reading.
README_STUB = (
    "jobs:\n"
    "  publish-docker-readme:\n"
    "    name: Publish Docker Hub readme job\n"
    "    permissions:\n      contents: read\n"
    "    uses: acme/hub/.github/workflows/publish-docker-readme-task.yml@" + "a" * 40 + " # 2.0.1\n"
    "    with:\n      branch: ${{ github.ref_name }}\n"
    "    secrets:\n"
    "      DOCKER_HUB_USERNAME: ${{ secrets.DOCKER_HUB_USERNAME }}\n"
    "      DOCKER_HUB_ACCESS_TOKEN: ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}\n"
)
README_CONTRACT = {
    "requiredJobKeys": ["publish-docker-readme"],
    "requireTokensInJob": {
        "publish-docker-readme": [
            "publish-docker-readme-task.yml",
            "contents: read",
            "DOCKER_HUB_USERNAME",
            "DOCKER_HUB_ACCESS_TOKEN",
        ]
    },
}
UPSTREAM_STUB = (
    "jobs:\n"
    "  check-upstream-version:\n"
    "    name: Check upstream version job\n"
    "    uses: acme/hub/.github/workflows/check-upstream-version-task.yml@"
    + "a"
    * 40
    + " # 2.0.1\n"
    "    secrets:\n"
    "      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}\n"
    "      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}\n"
)
UPSTREAM_CONTRACT = {
    "requiredJobKeys": ["check-upstream-version"],
    "requireTokensInJob": {
        "check-upstream-version": [
            "check-upstream-version-task.yml",
            "CODEGEN_APP_CLIENT_ID",
            "CODEGEN_APP_PRIVATE_KEY",
        ]
    },
}
CODEGEN_STUB = (
    "jobs:\n"
    "  run-codegen:\n"
    "    name: Run codegen and pull request job\n"
    "    uses: acme/hub/.github/workflows/run-codegen-pull-request-task.yml@"
    + "a"
    * 40
    + " # 2.0.1\n"
    "    secrets:\n"
    "      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}\n"
    "      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}\n"
)
CODEGEN_CONTRACT = {
    "requiredJobKeys": ["run-codegen"],
    "requireTokensInJob": {
        "run-codegen": [
            "run-codegen-pull-request-task.yml",
            "CODEGEN_APP_CLIENT_ID",
            "CODEGEN_APP_PRIVATE_KEY",
        ]
    },
}
INTERFACE_CASES += [
    case
    for prefix, stub, contract in (
        ("publish-docker-readme.yml", README_STUB, README_CONTRACT),
        ("check-upstream-version.yml", UPSTREAM_STUB, UPSTREAM_CONTRACT),
        ("run-codegen-pull-request.yml", CODEGEN_STUB, CODEGEN_CONTRACT),
    )
    for case in (
        (f"{prefix} caller stub reaching the hub task with secrets mapped", stub, contract, 0),
        (
            f"{prefix} copy still carrying the job body reports the missing caller job",
            "jobs:\n  some-other-job:\n    runs-on: ubuntu-latest\n    steps: []\n",
            contract,
            1,
        ),
    )
]

CODE_VIEW_CASES = [
    (
        "block scalar body dropped, key line kept",
        "  deploy:\n    name: |\n      with:\n      environment:\n",
        "deploy:\n  name:",
    ),
    (
        "a dedented sibling key ends the block scalar body",
        "  deploy:\n    name: |\n      with:\n    with:\n      environment: prod\n",
        "deploy:\n  name:\n  with:\n    environment: prod",
    ),
    (
        "folded scalar, strip-chomp indicator, trailing comment on the key line",
        "  deploy:\n    name: >-  # a folded, strip-chomped scalar\n      environment:\n",
        "deploy:\n  name:",
    ),
    (
        "a step's `- run: |` block scalar body drops, its sibling env: mapping survives",
        "      - run: |\n          echo body\n        env:\n          TOKEN: xyz\n",
        "  run:\n  env:\n    TOKEN: xyz",
    ),
    (
        "an anchored block scalar (`name: &label |`) is still recognized as a block-scalar key",
        "  deploy:\n    name: &lbl |\n      with:\n      environment:\n",
        "deploy:\n  name:",
    ),
    (
        "a bare sequence-item block scalar (`- |`, a matrix string) still drops its body",
        "  include:\n    - |\n      TOKEN\n",
        "include:",
    ),
    (
        "a folded `if: >-` keeps its body, folded onto one line",
        "  publish:\n    if: >-\n      ${{ a\n\n      && b }}\n    uses: x\n",
        "publish:\n  if: ${{ a && b }}\n  uses: x",
    ),
    (
        "a step's folded `- if: >-` keeps its body too",
        "      - if: >-\n          ${{ a }}\n        run: echo\n",
        "  if: ${{ a }}\n  run: echo",
    ),
]


class CheckInterfaceCase(unittest.TestCase):
    """Holds the check_interface finding counts over synthetic workflow stubs."""

    def test_finding_counts(self) -> None:
        for label, text, contract, want in INTERFACE_CASES:
            with self.subTest(label):
                self.assertEqual(len(audit.check_interface("wf", contract, text)), want)

    def test_the_off_pull_request_finding_names_the_job_beside_a_gated_one(self) -> None:
        text = (
            PR_HEAD.replace("on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n")
            + PR_CHECK.replace(PR_CHECK_NAME, PR_ONLY_EXPRESSION)
            + PR_CHECK.replace("check-workflow-status:", "extra:")
        )
        [(kind, message)] = audit.check_interface("wf", PR_CONTRACT, text)
        self.assertEqual(kind, "DRIFT")
        self.assertIn("job 'extra'", message)
        self.assertNotIn("check-workflow-status", message)

    def test_an_every_event_expression_on_a_dispatch_workflow_is_its_job_finding(self) -> None:
        both = "' || '" + PR_CHECK_NAME + "' }}"
        text = PR_HEAD.replace(
            "on: pull_request\n", "on:\n  pull_request:\n  workflow_dispatch:\n"
        ) + PR_CHECK.replace(
            PR_CHECK_NAME, PR_ONLY_EXPRESSION.replace("' || 'Check workflow status job' }}", both)
        )
        [(kind, message)] = audit.check_interface("wf", PR_CONTRACT, text)
        self.assertEqual(kind, "DRIFT")
        self.assertIn("job 'check-workflow-status'", message)
        self.assertIn("on a run other than pull_request", message)

    def test_a_gate_without_a_pull_request_trigger_gets_the_trigger_finding(self) -> None:
        on = "on:\n  push:\n    branches: ['**']\n  workflow_dispatch:\n"
        for label, check, others in (
            ("literal name", PR_CHECK, ["on a run other than pull_request"]),
            ("pull_request-only name", PR_CHECK.replace(PR_CHECK_NAME, PR_ONLY_EXPRESSION), []),
        ):
            with self.subTest(label):
                text = PR_HEAD.replace("on: pull_request\n", on) + check
                trigger, *rest = audit.check_interface("wf", PR_CONTRACT, text)
                self.assertEqual(trigger[0], "DRIFT")
                self.assertIn("has no pull_request trigger", trigger[1])
                self.assertEqual(len(rest), len(others))
                for (_, message), want in zip(rest, others, strict=True):
                    self.assertIn(want, message)


class ShippedPullRequestStubCase(unittest.TestCase):
    """Every pull request stub the hub ships passes the contract spec/files.json gives a carried one."""

    def test_shipped_stubs_meet_the_carried_contract(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        spec = json.loads((root / "spec/files.json").read_text(encoding="utf-8"))
        path = ".github/workflows/test-pull-request.yml"
        contract = next(
            unit["contract"]
            for units in spec.values()
            if isinstance(units, list)
            for unit in units
            if isinstance(unit, dict) and unit.get("path") == path
        )
        for stub in (
            path,
            "catalog/snippets/workflows/test-pull-request.yml",
            "catalog/snippets/workflows/test-pull-request-smoke.yml",
        ):
            with self.subTest(stub):
                text = (root / stub).read_text(encoding="utf-8")
                self.assertEqual(audit.check_interface(stub, contract, text), [])
                self.assertIn(PR_ONLY_EXPRESSION, text)


class ForbiddenTokenCase(unittest.TestCase):
    """A forbidden token anchored on a newline matches the job's first key too."""

    def test_job_level_environment_as_the_first_key(self) -> None:
        text = (
            "jobs:\n  deploy:\n    environment: production\n"
            "    uses: acme/hub/.github/workflows/deploy-site-task.yml@x\n"
        )
        contract = {"forbidTokensInJob": {"deploy": ["\n    environment:"]}}
        self.assertEqual(len(audit.check_interface("wf", contract, text)), 1)


class SplitJobsCase(unittest.TestCase):
    """Holds the split_jobs edge cases."""

    def test_a_sibling_key_ends_the_last_job(self) -> None:
        text = "jobs:\n  a:\n    runs-on: x\n\n  b:\n    runs-on: y\nenv:\n  K: v\n"
        self.assertEqual(
            audit.split_jobs(text), {"a": "  a:\n    runs-on: x\n\n", "b": "  b:\n    runs-on: y\n"}
        )

    def test_the_last_job_keeps_a_final_line_with_no_newline(self) -> None:
        self.assertEqual(
            audit.split_jobs("jobs:\n  a:\n    runs-on: x"), {"a": "  a:\n    runs-on: x"}
        )

    def test_trailing_comment(self) -> None:
        trailing = audit.split_jobs(REL_OK + "# a trailing top-level comment\n")
        self.assertEqual(set(trailing), {"get-version", "build-widget", "github-release"})
        self.assertNotIn(
            "trailing top-level comment",
            trailing.get("github-release", ""),
            "split_jobs (trailing comment)",
        )

    def test_inline_mapping(self) -> None:
        inline = audit.split_jobs(
            "name: X\non: push\njobs:\n  quick: {runs-on: ubuntu-latest}\n  full:\n    runs-on: ubuntu-latest\n"
        )
        self.assertEqual(set(inline), {"quick", "full"})
        self.assertIn("runs-on", inline.get("quick", ""), "split_jobs (inline mapping)")


class CodeViewCase(unittest.TestCase):
    """Holds the block-scalar handling of code_view."""

    def test_block_scalar_handling(self) -> None:
        for label, text, want in CODE_VIEW_CASES:
            with self.subTest(label):
                self.assertEqual(audit.code_view(audit._compose(text)), want)

    def test_unparsed_workflow_is_one_finding(self) -> None:
        got = audit.check_interface("wf", PR_CONTRACT, "jobs:\n  a: [\n")
        self.assertEqual(
            got, [("DRIFT", "interface: wf does not parse as YAML, so its contract is unread")]
        )
        publisher = ".github/workflows/publish-release.yml"
        self.assertEqual(audit.concurrency_caller_findings(publisher, "jobs:\n  a: [\n"), [])
        entry = {"types": ["python"], "pythonDirectories": ["x"]}
        self.assertEqual(
            audit.python_directories_caller_findings(publisher, "jobs:\n  a: [\n", entry), []
        )


CANON = "line one\nline two\nline three\n"
PIN_A = "      - uses: actions/checkout@" + "a" * 40 + " # v7.0.0\n"
PIN_B = "      - uses: actions/checkout@" + "B" * 40 + " # v7.0.1\n"
PIN_STRUCT = "      - uses: actions/setup-node@" + "a" * 40 + " # v7.0.0\n"
NOTE_X = "      - uses: actions/checkout@" + "a" * 40 + " # kept for the audited build\n"
NOTE_Y = "      - uses: actions/checkout@" + "a" * 40 + " # kept for a different reason\n"
D3 = "2024-03-01T00:00:00+00:00"
D2 = "2024-02-01T00:00:00+00:00"
D1 = "2024-01-01T00:00:00+00:00"
IDENTITY = (
    ["git", "config", "user.email", "test@test.invalid"],
    ["git", "config", "user.name", "test"],
    ["git", "config", "commit.gpgsign", "false"],
)


class VerbatimCase(unittest.TestCase):
    """Holds classify_verbatim and the content_hash normalization checks."""

    def test_classify_verbatim(self) -> None:
        cases = [
            # (label, down_text, canon_text, history, want)
            ("identical -> match", CANON, CANON, [], None),
            ("EOL-only diff (CRLF) -> match", CANON.replace("\n", "\r\n"), CANON, [], None),
            ("EOL-only diff (bare CR) -> match", CANON.replace("\n", "\r"), CANON, [], None),
            (
                "body edit -> modified",
                CANON.replace("line two", "line TWO edited"),
                CANON,
                [],
                "modified",
            ),
            (
                "matches a past revision -> stale",
                "old body\n",
                "current body\n",
                ["old body\n", "older\n"],
                "stale",
            ),
            (
                "matches a past revision modulo EOL -> stale",
                "old body\r\n",
                "current body\n",
                ["old body\n"],
                "stale",
            ),
            (
                "edit in no revision -> modified",
                "never existed\n",
                "current body\n",
                ["old body\n"],
                "modified",
            ),
        ]
        for label, down, canon_t, history, want in cases:
            with self.subTest(label):
                self.assertEqual(audit.classify_verbatim(down, canon_t, history), want)

    def test_action_pin_normalization(self) -> None:
        self.assertEqual(
            audit.content_hash(PIN_A),
            audit.content_hash(PIN_B),
            "action-pin: a uses:@<sha> bump (sha + # vN, uppercase hex) should normalize equal",
        )
        self.assertNotEqual(
            audit.content_hash(PIN_A),
            audit.content_hash(PIN_STRUCT),
            "action-pin: a changed action name must still hash differently",
        )
        self.assertNotEqual(
            audit.content_hash(NOTE_X),
            audit.content_hash(NOTE_Y),
            "action-pin: a hand-written (non-version) pin comment must survive to be compared",
        )

    def test_needs_mask(self) -> None:
        needs_full = (
            "  github-release:\n"
            "    needs: [get-version, validate-release, build-nuget, dotnet-publish]\n"
            "    runs-on: x\n    steps: []\n"
        )
        needs_pruned = (
            "  github-release:\n"
            "    needs: [get-version, validate-release, dotnet-publish]\n"
            "    runs-on: x\n    steps: []\n"
        )
        needs_block = (
            "  github-release:\n    needs:\n      - get-version\n      - dotnet-publish\n"
            "    runs-on: x\n    steps: []\n"
        )
        needs_scalar = (
            "  github-release:\n    needs: dotnet-publish\n    runs-on: x\n    steps: []\n"
        )
        needs_forked = (
            "  github-release:\n    needs: [get-version, validate-release]\n"
            "    runs-on: x\n    steps:\n      - run: fork\n"
        )
        self.assertEqual(
            len(
                {
                    audit.content_hash(needs_full),
                    audit.content_hash(needs_pruned),
                    audit.content_hash(needs_block),
                    audit.content_hash(needs_scalar),
                }
            ),
            1,
            "needs-mask: a pruned needs list (inline, block, or scalar) should normalize equal",
        )
        self.assertNotEqual(
            audit.content_hash(needs_full),
            audit.content_hash(needs_forked),
            "needs-mask: a step change must still hash differently",
        )
        self.assertIn(
            "runs-on: x",
            audit.normalize(needs_block),
            "needs-mask: masking a block needs list must not consume the next key",
        )


class LastEffectiveChangeCase(unittest.TestCase):
    """Holds _last_effective_change, which skips a normalized-only bump."""

    def test_last_effective_change(self) -> None:
        cases = [
            (
                "every bump back to creation is pin-only -> creation wins",
                [(D3, "sha3", PIN_B), (D2, "sha2", PIN_A), (D1, "sha1", PIN_A)],
                (D1, "sha1"),
            ),
            (
                "newest differs for real -> newest wins",
                [(D3, "sha3", PIN_STRUCT), (D2, "sha2", PIN_A), (D1, "sha1", PIN_A)],
                (D3, "sha3"),
            ),
            (
                "newest is a pin-only bump over a real change -> the real change wins",
                [(D3, "sha3", PIN_B), (D2, "sha2", PIN_A), (D1, "sha1", PIN_STRUCT)],
                (D2, "sha2"),
            ),
            (
                "one revision (creation) -> that revision wins, nothing to compare against",
                [(D1, "sha1", "only revision\n")],
                (D1, "sha1"),
            ),
            (
                "unreadable newest text -> treated as effective, never silently skipped",
                [(D2, "sha2", None), (D1, "sha1", "whatever\n")],
                (D2, "sha2"),
            ),
            ("no history -> None", [], None),
        ]
        for label, revisions, want in cases:
            with self.subTest(label):
                self.assertEqual(audit._last_effective_change(revisions), want)


class GitRepoCase(unittest.TestCase):
    """Base for cases that build temporary git repositories."""

    def run_git(self, cwd: pathlib.Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def make_dir(self) -> pathlib.Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name)

    def make_repo(self, *init_args: str) -> pathlib.Path:
        path = self.make_dir()
        self.run_git(path, "init", "-q", *init_args)
        for cmd in IDENTITY:
            self.run_git(path, *cmd[1:])
        return path


class GitRevisionsCase(GitRepoCase):
    """Holds _git_revisions against temporary git repositories."""

    def revision_texts(self, root: pathlib.Path, rel: str, **kwargs: str) -> list[str | None]:
        with mock.patch.object(audit, "ROOT", root):
            audit._git_revisions.cache_clear()
            self.addCleanup(audit._git_revisions.cache_clear)
            revisions = audit._git_revisions(rel, **kwargs)
        return [text for _, _, text in revisions]

    def test_re_added_file(self) -> None:
        # A deletion revision reads as None even once rel_path exists again in a later commit.
        root = self.make_repo()
        rel = "deletion-probe.txt"
        (root / rel).write_text("v1\n")
        self.run_git(root, "add", rel)
        self.run_git(root, "commit", "-q", "-m", "add")
        self.run_git(root, "rm", "-q", rel)
        self.run_git(root, "commit", "-q", "-m", "delete")
        (root / rel).write_text("v2\n")
        self.run_git(root, "add", rel)
        self.run_git(root, "commit", "-q", "-m", "readd")
        self.assertEqual(
            self.revision_texts(root, rel, rev="HEAD"),
            ["v2\n", None, "v1\n"],
            "_git_revisions: re-added file",
        )

    def test_file_to_directory_transition(self) -> None:
        root = self.make_repo()
        rel = "thing"
        (root / rel).write_text("v1\n")
        self.run_git(root, "add", rel)
        self.run_git(root, "commit", "-q", "-m", "file")
        self.run_git(root, "rm", "-q", rel)
        (root / rel).mkdir()
        (root / rel / "inner.txt").write_text("v2\n")
        self.run_git(root, "add", rel)
        self.run_git(root, "commit", "-q", "-m", "now a directory")
        self.assertEqual(
            self.revision_texts(root, rel, rev="HEAD"),
            [None, "v1\n"],
            "_git_revisions: file-to-directory transition",
        )

    def test_file_to_symlink_transition(self) -> None:
        root = self.make_repo()
        rel = "thing"
        (root / rel).write_text("v1\n")
        self.run_git(root, "add", rel)
        self.run_git(root, "commit", "-q", "-m", "file")
        self.run_git(root, "rm", "-q", rel)
        try:
            (root / rel).symlink_to("target")
        except OSError:
            self.skipTest(
                "_git_revisions: file-to-symlink transition (host cannot create symlinks)"
            )
        self.run_git(root, "add", rel)
        self.run_git(root, "commit", "-q", "-m", "now a symlink")
        self.assertEqual(
            self.revision_texts(root, rel, rev="HEAD"),
            [None, "v1\n"],
            "_git_revisions: file-to-symlink transition",
        )

    def test_default_rev_reads_origin_main(self) -> None:
        # `origin` is a plain local path here, so the fetch stays offline.
        upstream = self.make_repo("-b", "main")
        root = self.make_dir()
        rel = "hub-probe.txt"
        (upstream / rel).write_text("main-content\n")
        self.run_git(upstream, "add", rel)
        self.run_git(upstream, "commit", "-q", "-m", "add on main")
        subprocess.run(
            ["git", "clone", "-q", str(upstream), str(root)], check=True, capture_output=True
        )
        # A clone carries no committer identity of its own, so it needs the same setup as upstream.
        for cmd in IDENTITY:
            self.run_git(root, *cmd[1:])
        self.run_git(root, "checkout", "-q", "-b", "develop")
        (root / rel).write_text("develop-only-content\n")
        self.run_git(root, "add", rel)
        self.run_git(root, "commit", "-q", "-m", "develop-only change")
        with mock.patch.object(audit, "ROOT", root):
            audit._git_revisions.cache_clear()
            audit._hub_main_rev.cache_clear()
            self.addCleanup(audit._git_revisions.cache_clear)
            self.addCleanup(audit._hub_main_rev.cache_clear)
            revisions = audit._git_revisions(rel)
        self.assertEqual(
            [text for _, _, text in revisions],
            ["main-content\n"],
            "_git_revisions: default rev reads origin's main, not the checked-out branch",
        )


class HubTrackedCase(GitRepoCase):
    """Holds hub_tracked against a filename the active locale rejects."""

    def test_non_utf8_filename_round_trips(self) -> None:
        # The ls-tree output is NUL-delimited raw bytes, so decoding before the NUL-split would crash.
        encoding = locale.getpreferredencoding(False)
        invalid_bytes = None
        for candidate in (b"\xff", b"\x80\x81", b"\xfe\xff"):
            try:
                candidate.decode(encoding)
            except UnicodeDecodeError:
                invalid_bytes = candidate
                break
        if invalid_bytes is None:
            self.skipTest(f"hub_tracked: no candidate byte sequence is invalid under {encoding!r}")
        bad_name = "bad-" + invalid_bytes.decode("utf-8", errors="surrogateescape") + "-name.txt"
        root = self.make_repo("-b", "main")
        (root / "control.txt").write_text("x")
        self.run_git(root, "add", "--", "control.txt")
        try:
            (root / bad_name).write_text("x")
        except OSError:
            self.skipTest("hub_tracked: non-UTF-8 filename (host cannot create it)")
        try:
            self.run_git(root, "add", "--", bad_name)
        except subprocess.CalledProcessError:
            if not IS_WINDOWS:
                raise
            self.skipTest("hub_tracked: non-UTF-8 filename (git cannot add it)")
        self.run_git(root, "commit", "-q", "-m", "add invalid-utf8 name")
        with mock.patch.object(audit, "ROOT", root):
            audit.hub_tracked.cache_clear()
            self.addCleanup(audit.hub_tracked.cache_clear)
            tracked = audit.hub_tracked(rev="HEAD")
        self.assertTrue(
            bad_name in tracked,
            "hub_tracked: a non-UTF-8 filename round-trips instead of crashing",
        )


def _probe_run(add: Any, commit: Any = None, control: Any = None) -> Any:
    """A subprocess.run stand-in answering the probe's own git add of the bad name with `add`."""
    real_run = subprocess.run

    def run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        if control is not None and cmd == ["git", "add", "--", "control.txt"]:
            return control(cmd)
        if cmd[:3] == ["git", "add", "--"] and kwargs.get("cwd") and cmd[3].startswith("bad-"):
            return add(cmd)
        if commit is not None and cmd[:2] == ["git", "commit"] and "add invalid-utf8 name" in cmd:
            return commit(cmd)
        return real_run(cmd, *args, **kwargs)

    return run


def _fail(cmd: list[str]) -> Any:
    raise subprocess.CalledProcessError(128, cmd)


class HubTrackedProbeCase(unittest.TestCase):
    """The invalid-byte probe skips only where git on Windows refuses the name, and fails loudly otherwise."""

    def probe(self, run: Any, windows: bool) -> BaseException:
        case = HubTrackedCase("test_non_utf8_filename_round_trips")
        self.addCleanup(case.doCleanups)
        with (
            mock.patch.object(sys.modules[__name__], "IS_WINDOWS", windows),
            mock.patch.object(subprocess, "run", run),
        ):
            try:
                case.test_non_utf8_filename_round_trips()
            except (unittest.SkipTest, subprocess.CalledProcessError) as error:
                if "host cannot create it" in str(error) or "no candidate byte" in str(error):
                    self.skipTest("this host cannot exercise the invalid-byte probe")
                return error
        self.fail("the probe neither skipped nor failed")

    def test_a_failing_add_of_the_probe_file_skips_on_windows(self) -> None:
        error = self.probe(_probe_run(_fail), windows=True)
        self.assertIsInstance(error, unittest.SkipTest)
        self.assertIn("git cannot add it", str(error))

    def test_a_failing_control_add_fails_loudly_on_windows(self) -> None:
        ok = _probe_run(lambda cmd: subprocess.CompletedProcess(cmd, 0), control=_fail)
        error = self.probe(ok, windows=True)
        assert isinstance(error, subprocess.CalledProcessError)
        self.assertEqual(error.cmd, ["git", "add", "--", "control.txt"])

    def test_a_failing_add_of_the_probe_file_fails_loudly_elsewhere(self) -> None:
        error = self.probe(_probe_run(_fail), windows=False)
        assert isinstance(error, subprocess.CalledProcessError)
        self.assertEqual(error.cmd[:3], ["git", "add", "--"])

    def test_another_git_failure_still_fails_loudly(self) -> None:
        run = _probe_run(lambda cmd: subprocess.CompletedProcess(cmd, 0), commit=_fail)
        error = self.probe(run, windows=True)
        assert isinstance(error, subprocess.CalledProcessError)
        self.assertEqual(error.cmd[:2], ["git", "commit"])


GIT_IDENTITY = (["git", "init", "-q"], *IDENTITY)


def clear_history_caches() -> None:
    audit._require_full_history.cache_clear()
    audit._git_revisions.cache_clear()
    audit.git_blob_in_file_history.cache_clear()


class SectionCase(unittest.TestCase):
    """Holds region splitting, section extraction and stripping, and fence handling."""

    def test_forked_release_region_hashes_differently(self) -> None:
        region = audit.split_jobs(REL_OK).get("github-release")
        forked_region = audit.split_jobs(
            REL_OK.replace("          merge-multiple: true\n", "          artifact-ids: 1\n")
        ).get("github-release")
        self.assertIsNotNone(
            region, "verbatim: forked github-release region should hash differently"
        )
        self.assertIsNotNone(
            forked_region, "verbatim: forked github-release region should hash differently"
        )
        self.assertNotEqual(
            audit.content_hash(region),
            audit.content_hash(forked_region),
            "verbatim: forked github-release region should hash differently",
        )

    def test_extract_section_region_and_hash(self) -> None:
        md = "# Title\n\n## Alpha\n\nbody a\n\n```\n## not a heading\n```\n\n### nested\nstill alpha\n\n## Beta\n\nbody b\n"
        a, b, gone = (
            audit.extract_section(md, "Alpha"),
            audit.extract_section(md, "Beta"),
            audit.extract_section(md, "Gamma"),
        )
        spaced = audit.extract_section("##   Alpha\n\nbody a\n", "Alpha")
        msg = "section: extract_section region/hash behavior"
        self.assertIsNotNone(a, msg)
        self.assertTrue(a.startswith("## Alpha"), msg)
        self.assertIn("body a", a, msg)
        self.assertIn("## not a heading", a, msg)
        self.assertIn("still alpha", a, msg)
        self.assertNotIn("body b", a, msg)
        self.assertIsNotNone(b, msg)
        self.assertTrue(b.startswith("## Beta"), msg)
        self.assertIn("body b", b, msg)
        self.assertNotIn("body a", b, msg)
        self.assertIsNone(gone, msg)
        self.assertIsNotNone(spaced, msg)
        self.assertTrue(spaced.startswith("##   Alpha"), msg)
        self.assertNotEqual(
            audit.content_hash(a),
            audit.content_hash(audit.extract_section(md.replace("## Alpha", "## alpha"), "Alpha")),
            msg,
        )

    def test_mismatched_marker_in_fence_is_not_a_boundary(self) -> None:
        nested = "## Alpha\n```md\n~~~\n## Fake\n```\nreal content\n"
        nested_extract = audit.extract_section(nested, "Alpha")
        nested_strip = audit.strip_sections(
            "## Keep\ntext\n```md\n~~~\n## Fake Drop\n```\nmore text\n## Drop\nreal drop content\n",
            ["Drop"],
        )
        msg = "section: a mismatched marker nested in a fence ends the region early"
        self.assertIsNotNone(nested_extract, msg)
        self.assertIn("## Fake", nested_extract, msg)
        self.assertIn("real content", nested_extract, msg)
        self.assertIn("## Fake Drop", nested_strip, msg)
        self.assertNotIn("real drop content", nested_strip, msg)
        self.assertIn("more text", nested_strip, msg)

    def test_unfenced_text(self) -> None:
        cases = [
            (
                "a simple ``` fence excludes its content",
                "```\n## Phantom\n```\n## Real\n",
                "## Real\n",
            ),
            (
                "a simple ~~~ fence excludes its content",
                "~~~\n## Phantom\n~~~\n## Real\n",
                "## Real\n",
            ),
            (
                "a ~~~ line nested inside a ``` fence does not close it",
                "```md\n~~~\n## Phantom\n```\n## Real\n",
                "## Real\n",
            ),
            (
                "a shorter ``` cannot close a longer ```` fence",
                "````md\n## Phantom\n```\n## Real\n````\n",
                "",
            ),
            (
                "a longer closing fence than the opener still closes",
                "```\n## Phantom\n````\n## Real\n",
                "## Real\n",
            ),
            (
                "a marker run followed by trailing text does not close",
                "```\n## Phantom\n```not-a-fence\n## Real\n```\n## Real2\n",
                "## Real2\n",
            ),
            (
                "trailing whitespace after the marker run still closes",
                "```\n## Phantom\n```   \n## Real\n",
                "## Real\n",
            ),
            (
                "a 3-space-indented closing fence still closes",
                "```\n## Phantom\n   ```\n## Real\n",
                "## Real\n",
            ),
            (
                "a 4-space-indented closing fence remains content, not a boundary",
                "```\n## Phantom\n    ```\n## Still Phantom\n```\n## Real\n",
                "## Real\n",
            ),
            (
                "a 4-space-indented opener never opens a fence",
                "    ```\n## Real\n",
                "    ```\n## Real\n",
            ),
            (
                "a backtick in a backtick-fence info string is not a valid opener",
                "```md`\n## Real\n",
                "```md`\n## Real\n",
            ),
            (
                "a backtick in a tilde-fence info string does not disqualify it",
                "~~~md`\n## Phantom\n~~~\n## Real\n",
                "## Real\n",
            ),
        ]
        for label, doc, want in cases:
            with self.subTest(label):
                self.assertEqual(audit_markdown.unfenced_text(doc), want)

    def test_undeclared_h2_headings(self) -> None:
        cases = [
            (
                "a declared H2 is not flagged",
                "# AGENTS\n\n## Fleet Bootstrap\n\nText.\n",
                {"fleet bootstrap"},
                [],
            ),
            (
                "an undeclared H2 is flagged",
                "# AGENTS\n\n## Fleet Bootstrap\n\nText.\n\n## Local Notes\n\nRepo-specific.\n",
                {"fleet bootstrap"},
                ["local notes"],
            ),
            (
                "declared-name match is case-insensitive",
                "# AGENTS\n\n## fleet BOOTSTRAP\n\nText.\n",
                {"fleet bootstrap"},
                [],
            ),
            (
                "an un-normalized declared set (mixed case, untrimmed) is normalized here, not trusted",
                "# AGENTS\n\n## Fleet Bootstrap\n\nText.\n",
                {" Fleet Bootstrap "},
                [],
            ),
            (
                "an H1 title and a nested H3 are not judged, only H2",
                "# Local Notes\n\n## Fleet Bootstrap\n\n### Local Notes\n\nText.\n",
                {"fleet bootstrap"},
                [],
            ),
            (
                "a fenced sample showing heading syntax is not a real heading",
                "# AGENTS\n\n## Fleet Bootstrap\n\n```\n## Local Notes\n```\n",
                {"fleet bootstrap"},
                [],
            ),
            (
                "a backtick in a backtick-fence info string is not a valid opener, so the heading after it is real",
                "# AGENTS\n\n## Fleet Bootstrap\n\n```md`\n## Local Notes\n",
                {"fleet bootstrap"},
                ["local notes"],
            ),
            (
                "a repo's own local content, undeclared, is flagged even in a file with its own declared sections",
                (
                    "# Copilot Instructions\n\n## GitHub Copilot Review Runbook\n\nText.\n\n"
                    "## Development Workflow\n\nLocal build and test steps.\n\n"
                    "## Command Line Usage\n\nLocal CLI reference.\n"
                ),
                {"github copilot review runbook"},
                ["command line usage", "development workflow"],
            ),
        ]
        for label, doc, declared, want in cases:
            with self.subTest(label):
                self.assertEqual(audit_markdown.undeclared_h2_headings(doc, declared), want)


class ScanCase(unittest.TestCase):
    """Holds the template-reference and version-literal scans outside verbatim sections."""

    def test_template_ref_outside_verbatim(self) -> None:
        boot = "## Fleet Bootstrap\n\nThe canonical rules live in `github.com/acme/Hub`.\n"
        owned = "## Where the Rules Live\n\nReport a rule discrepancy to acme/Hub.\n"
        clean_doc = (
            "# AGENTS\n\n"
            + boot
            + "\n## Where the Rules Live\n\nState the behavior, not the destination.\n"
        )
        dirty_doc = "# AGENTS\n\n" + boot + "\n" + owned
        cases = [
            ("hub name only inside the verbatim section", clean_doc, {"Fleet Bootstrap"}, False),
            ("hub name in prose the repo owns", dirty_doc, {"Fleet Bootstrap"}, True),
            ("same document with nothing declared verbatim still flags", clean_doc, set(), True),
            (
                "CRLF document excises the same way",
                clean_doc.replace("\n", "\r\n"),
                {"Fleet Bootstrap"},
                False,
            ),
            (
                "a re-cased verbatim heading still excises",
                clean_doc.replace("## Fleet Bootstrap", "## fleet bootstrap"),
                {"Fleet Bootstrap"},
                False,
            ),
            (
                "no hub reference at all",
                "# AGENTS\n\n## Where the Rules Live\n\nNothing to see.\n",
                {"Fleet Bootstrap"},
                False,
            ),
            (
                "the heading appearing twice excises both",
                "# AGENTS\n\n" + boot + "\n## Notes\n\n" + boot,
                {"Fleet Bootstrap"},
                False,
            ),
            (
                "a fenced copy of the section is prose, not the section",
                "# AGENTS\n\n## Notes\n\n```\n" + boot + "```\n\n" + boot,
                {"Fleet Bootstrap"},
                True,
            ),
        ]
        for label, doc, verb, want in cases:
            with self.subTest(label):
                self.assertEqual(audit.template_ref_outside_verbatim(doc, verb, "acme/Hub"), want)

    def test_version_literals_outside_verbatim(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        cases = [
            (
                "a release number is flagged",
                "Pinned at hub release `2.0.657`.\n",
                set(),
                ["2.0.657"],
            ),
            ("a full SHA is flagged", f"uses: o/r/x.yml@{sha} # 2.0.1\n", set(), ["2.0.1", sha]),
            ("an uppercase SHA is flagged", f"Pinned at {sha.upper()}.\n", set(), [sha.upper()]),
            ("an abbreviated SHA is flagged", "Pinned at `f3b4cc9`.\n", set(), ["f3b4cc9"]),
            (
                "an uppercase abbreviated SHA is flagged",
                "Pinned at `F3B4CC9`.\n",
                set(),
                ["F3B4CC9"],
            ),
            ("an all-letter hex word is not a SHA", "A facade over deadbeef.\n", set(), []),
            ("a hex color is not a SHA", "Color #1f2937ff.\n", set(), []),
            ("a hash-prefixed hex token is not a SHA", "See #f3b4cc9.\n", set(), []),
            (
                "an identifier suffix is not a SHA",
                "Name foo_1a2b3c4d and 123e4567-e89b.\n",
                set(),
                [],
            ),
            ("an image tag SHA is flagged", "Image app:sha-1a2b3c4.\n", set(), ["1a2b3c4"]),
            ("a hex constant is not a SHA", "Mask 0x7fffffff.\n", set(), []),
            (
                "a worked example is flagged",
                "1.0.12 publishes as 1.0.13.\n",
                set(),
                ["1.0.12", "1.0.13"],
            ),
            ("a placeholder example is clean", "`1.0.N` publishes as `1.0.(N+1)`.\n", set(), []),
            ("the mechanism alone is clean", "SHA-pinned to a hub release.\n", set(), []),
            ("a dotted quad is not a version", "Host 192.168.1.10 serves it.\n", set(), []),
            ("a two-part version is not a pin", "Python 3.13 and 3.14.\n", set(), []),
            (
                "a literal inside a verbatim section is the hub's",
                "# W\n\n## Locked\n\nPinned at 2.0.1.\n\n## Mine\n\nClean.\n",
                {"Locked"},
                [],
            ),
            (
                "a literal outside the verbatim section is flagged",
                "# W\n\n## Locked\n\nClean.\n\n## Mine\n\nPinned at 2.0.2.\n",
                {"Locked"},
                ["2.0.2"],
            ),
        ]
        for label, doc, verb, want in cases:
            with self.subTest(label):
                self.assertEqual(audit.version_literals_outside_verbatim(doc, verb), sorted(want))


class RenderCase(unittest.TestCase):
    """Holds the issue generator, the README mirror check, link stripping, and cspell detection."""

    def test_render_issue_grouping(self) -> None:
        fe = {"name": "Widget", "types": ["python"]}
        it, ib = audit.render_issue(
            fe,
            [
                ("LETTER", "file: X absent"),
                ("DRIFT", "verbatim: Y differs"),
                ("ERROR", "gh failed"),
            ],
            "main",
            "abc1234",
            "2026-01-01T00:00:00Z",
            "hub123",
        )
        et, eb = audit.render_issue(fe, [], "main", "abc1234", "2026-01-01T00:00:00Z", "hub123")
        msg = "issue: render_issue grouping/title/empty behavior"
        self.assertIn("Widget", it, msg)
        self.assertIn("(3 detected findings)", it, msg)
        self.assertIn("## Must fix", ib, msg)
        self.assertIn("**LETTER** file: X absent", ib, msg)
        self.assertIn("## Converge", ib, msg)
        self.assertIn("verbatim: Y differs", ib, msg)
        self.assertIn("## Could not verify", ib, msg)
        self.assertIn("(0 detected findings)", et, msg)
        self.assertIn("nothing to converge", eb, msg)

    def test_render_issue_ends_at_the_manual_findings_section(self) -> None:
        """Both bodies end in the one hand-written section, so regenerating can keep it, per AUDIT.md section 8."""
        fe = {"name": "Widget", "types": ["python"]}
        for findings in ([], [("DRIFT", "verbatim: Y differs"), ("ERROR", "gh failed")]):
            with self.subTest(findings=len(findings)):
                _, body = audit.render_issue(
                    fe, findings, "main", "abc1234", "2026-01-01T00:00:00Z", "hub123"
                )
                headings = [line for line in body.splitlines() if line.startswith("## ")]
                self.assertEqual(headings[-1], audit.MANUAL_HEADING)
                self.assertEqual(headings.count(audit.MANUAL_HEADING), 1)
                self.assertIn(f"replace only the text above `{audit.MANUAL_HEADING}`", body)

    def test_title_and_intro_mirror(self) -> None:
        r_md = "# Widget <!-- omit from toc -->\n\nDoes widget things.\n\n## Build\n"
        h_md = "# Widget\n\nDoes widget things.\n\n## Release History\n"
        h_bad = "# Widget\n\nDoes other things.\n\n## Release History\n"
        r_two = "# W\n\nLine one.\n\nLine two.\n\n## Build\n"
        h_joined = "# W\n\nLine one.\nLine two.\n\n## Release History\n"
        msg = "history: README/HISTORY title+intro mirror detection"
        self.assertEqual(
            audit_markdown.title_and_intro(r_md), audit_markdown.title_and_intro(h_md), msg
        )
        self.assertNotEqual(
            audit_markdown.title_and_intro(r_md), audit_markdown.title_and_intro(h_bad), msg
        )
        self.assertNotEqual(
            audit_markdown.title_and_intro(r_two), audit_markdown.title_and_intro(h_joined), msg
        )

    def test_strip_md_links(self) -> None:
        linked = "Utility to clean [media](https://x.example/Foo_(bar)) per the [spec][spec-ref]."
        nested = "See [API [docs]](https://example.test/a_(b)_(c)) for details."
        inner_link = "See [[docs](url)] for details."
        escaped_bracket = r"See [API \[docs](url) for details."
        msg = "description: strip_md_links behavior"
        refs = audit_markdown.references("x\n\n[spec-ref]: https://example.test\n")
        self.assertEqual(
            audit_markdown.strip_md_links(linked, refs), "Utility to clean media per the spec.", msg
        )
        self.assertEqual(
            audit_markdown.strip_md_links(linked),
            "Utility to clean media per the [spec][spec-ref].",
            msg,
        )
        self.assertEqual(
            audit_markdown.strip_md_links("Plain intro line."), "Plain intro line.", msg
        )
        padded = "Runs ` make ` targets with `` `b` ``."
        self.assertEqual(audit_markdown.strip_md_links(padded), padded, msg)
        linked_span = padded + " See [docs](u)."
        self.assertEqual(audit_markdown.strip_md_links(linked_span), padded + " See docs.", msg)
        imaged = "Cleans media ![ci](https://img.example/b.svg) fast."
        self.assertEqual(audit_markdown.strip_md_links(imaged), "Cleans media ci fast.", msg)
        self.assertEqual(audit_markdown.strip_md_links(nested), "See API [docs] for details.", msg)
        self.assertEqual(audit_markdown.strip_md_links(inner_link), "See [docs] for details.", msg)
        self.assertEqual(
            audit_markdown.strip_md_links(escaped_bracket), r"See API \[docs for details.", msg
        )

    def test_strip_md_links_stays_linear(self) -> None:
        # A ratio rather than a wall-clock bound, since coverage tracing slows both runs alike.
        def cost(n: int) -> float:
            start = time.monotonic()
            audit_markdown.strip_md_links("[" * n)
            return time.monotonic() - start

        small, large = cost(5000), cost(20000)
        self.assertLess(
            large,
            8 * small + 0.05,
            f"description: strip_md_links took {small:.2f}s then {large:.2f}s on 4x the brackets, expected linear",
        )

    def test_workspace_cspell_words(self) -> None:
        ws_dup = '{ "settings": { "cSpell.words": ["foo"] } }'
        ws_ok = '{ "settings": { "editor.rulers": [100] }, "note": "words live in cspell.json" }'
        msg = "cspell: workspace word-list detection"
        self.assertTrue(audit.workspace_cspell_words(ws_dup), msg)
        self.assertFalse(audit.workspace_cspell_words(ws_ok), msg)


class BranchCase(unittest.TestCase):
    """Holds branch drift classification and ground-truth branch resolution."""

    def test_classify_branch_drift(self) -> None:
        bd_base = {"keep": "a", "moda": "1", "modb": "2", "deld": "e", "devonly": "x"}
        bd_main = {
            "keep": "a",
            "moda": "9",
            "modb": "9",
            "add": "n",
            "devonly": "x",
        }
        bd_dev = {
            "keep": "a",
            "moda": "1",
            "modb": "7",
            "add": "m",
            "deld": "e",
            "devonly": "y",
        }
        bd_behind, bd_diverged = audit.classify_branch_drift(bd_base, bd_main, bd_dev)
        self.assertEqual(
            (bd_behind, bd_diverged),
            (["deld", "moda"], ["add", "modb"]),
            f"branch-drift classify -> behind={bd_behind} diverged={bd_diverged}",
        )

    def test_ground_branch_of(self) -> None:
        cases = [
            ({}, None, None, "main"),
            ({"groundTruthBranch": "develop"}, None, None, "develop"),
            ({"groundTruthBranch": "main"}, "develop", None, "develop"),
            ({}, "feature/x", None, "feature/x"),
            ({}, None, {"groundTruthBranch": "develop"}, "develop"),
            ({"groundTruthBranch": "main"}, None, {"groundTruthBranch": "develop"}, "main"),
            ({"groundTruthBranch": ""}, None, {"groundTruthBranch": "develop"}, ""),
            ({}, None, {"groundTruthBranch": ""}, ""),
        ]
        for entry, branch, defaults, want in cases:
            with self.subTest(f"{entry} + {branch} + {defaults}"):
                self.assertEqual(audit.ground_branch_of(entry, branch, defaults), want)


class ShallowCloneCase(GitRepoCase):
    """Holds the shallow-clone refusal on the history reads and on the public paths."""

    def setUp(self) -> None:
        clear_history_caches()
        self.addCleanup(clear_history_caches)

    def test_history_reads_refuse_shallow_clone(self) -> None:
        tmp_path = self.make_dir()
        full = tmp_path / "full"
        full.mkdir()
        for cmd in (
            *GIT_IDENTITY,
            ["git", "commit", "-q", "--allow-empty", "-m", "one"],
            ["git", "commit", "-q", "--allow-empty", "-m", "two"],
        ):
            subprocess.run(cmd, cwd=full, check=True, capture_output=True)
        shallow = tmp_path / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", full.as_uri(), str(shallow)],
            check=True,
            capture_output=True,
        )
        for label, root, want_refuse in (("shallow", shallow, True), ("full", full, False)):
            with mock.patch.object(audit, "ROOT", root):
                clear_history_caches()
                reads = (
                    ("revisions", lambda: audit._git_revisions("absent.txt", rev="HEAD")),
                    (
                        "blob",
                        lambda: audit.git_blob_in_file_history("absent.txt", "0" * 40, rev="HEAD"),
                    ),
                )
                for read, call in reads:
                    with self.subTest(f"{label} clone, {read}"):
                        try:
                            call()
                            msg = None
                        except audit.ShallowCloneError as e:
                            msg = str(e)
                        good = (msg is not None) == want_refuse
                        if want_refuse:
                            good = (
                                good
                                and "Run `git fetch --unshallow origin` in that checkout."
                                in (msg or "")
                            )
                        self.assertTrue(good, f"shallow-clone guard: {label} clone, {read}")

    def test_public_paths_refuse_shallow_clone(self) -> None:
        tmp_path = self.make_dir()
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir()
        rels = (
            "registry/repos.json",
            "repo-config/settings.json",
            "spec/secrets.json",
            "spec/files.json",
            "spec/project-types.json",
            "spec/readme-sections.json",
            "spec/third-party-tools.json",
            "spec/divergences.json",
        )
        for rel in rels:
            (hub_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (hub_dir / rel).write_bytes((audit.ROOT / rel).read_bytes())
        for cmd in (
            *GIT_IDENTITY,
            ["git", "add", "."],
            ["git", "commit", "-q", "-m", "one"],
            ["git", "commit", "-q", "--allow-empty", "-m", "two"],
        ):
            subprocess.run(cmd, cwd=hub_dir, check=True, capture_output=True)
        shallow_hub = tmp_path / "shallow-hub"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", hub_dir.as_uri(), str(shallow_hub)],
            check=True,
            capture_output=True,
        )
        name = next(
            r["name"]
            for r in audit.load("registry/repos.json")["repos"]
            if r.get("status") == "cataloged"
        )

        def history_read_only(entry, spec, branch=None):
            audit._git_revisions("absent.txt", rev="HEAD")
            return [], ""

        with (
            mock.patch.object(audit, "ROOT", shallow_hub),
            mock.patch.object(audit, "audit_repo", history_read_only),
        ):
            for flag_argv, label, want_code in (
                ([name], "sweep", 1),
                (["--issue", name], "issue", 2),
            ):
                with self.subTest(label):
                    audit._require_full_history.cache_clear()
                    audit._git_revisions.cache_clear()
                    out, err = io.StringIO(), io.StringIO()
                    try:
                        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            code = audit.main(flag_argv)
                    except Exception as e:  # noqa: BLE001
                        code = f"raised {type(e).__name__}"
                    text = out.getvalue() if label == "sweep" else err.getvalue()
                    good = (
                        code == want_code
                        and "Run `git fetch --unshallow origin` in that checkout." in text
                        and (label == "sweep" or not out.getvalue())
                    )
                    if label == "sweep":
                        good = good and "ERROR" in text
                    self.assertTrue(
                        good, f"shallow-clone refusal, public {label} path (code {code})"
                    )


class ParseArgsCase(unittest.TestCase):
    """Holds command line parsing, where a repo name and a flag value stay distinct."""

    def test_parse_args(self) -> None:
        cases = [
            ([], [], None, False, False),
            (["Utilities"], ["Utilities"], None, False, False),
            (["Utilities", "--branch", "develop"], ["Utilities"], "develop", False, False),
            (["--branch=feature/x", "Utilities"], ["Utilities"], "feature/x", False, False),
            (["--issue", "Utilities"], ["Utilities"], None, True, False),
            (["--selftest"], [], None, False, True),
        ]
        for argv, names, branch, issue, selftest in cases:
            with self.subTest(str(argv or ["(no args)"])):
                got = audit.parse_args(argv)
                self.assertEqual(
                    (got.names, got.branch, got.issue, got.selftest),
                    (names, branch, issue, selftest),
                )


NOTE_SPEC = {
    "types": {
        "types": {
            "hugo": {"checks": [{"id": "hugo.generator.pinned"}]},
            "docker": {"checks": [{"id": "docker.cache.registry"}]},
        },
        "crossCutting": {"setup": {"checks": [{"id": "setup.driftnotes.current"}]}},
    }
}

NOTE_CASES = [
    # The end-of-sentence period is the case an end-anchored pattern misses, and it is how both notes this form was introduced for are written.
    (
        "check id followed by a period",
        ["hugo"],
        ["Pinned in two workflows (hugo.generator.pinned)."],
        0,
        ["does not evaluate"],
    ),
    (
        "check id mid-sentence",
        ["hugo"],
        ["The pin (hugo.generator.pinned) is duplicated."],
        0,
        ["does not evaluate"],
    ),
    (
        "cross-cutting id, which no repo declares",
        ["hugo"],
        ["Notes go stale (setup.driftnotes.current)."],
        0,
        ["does not evaluate"],
    ),
    (
        "check id of an undeclared type",
        ["hugo"],
        ["Cache layer (docker.cache.registry)."],
        0,
        ["this repo does not declare"],
    ),
    (
        "a malformed types value does not abort the note check",
        None,
        ["Vendored helper, not tracked (python.directories.declared)."],
        0,
        [],
    ),
    (
        "a detection suppression on a repo without the type",
        ["hugo"],
        ["Vendored helper, not tracked (python.directories.declared)."],
        0,
        [],
    ),
    (
        "check id absent from the catalog",
        ["hugo"],
        ["Theme record (hugo.vendored.provenence)."],
        0,
        ["neither spec/checks.json nor spec/project-types.json defines"],
    ),
    # A version string in parentheses is the false positive the id shape has to exclude.
    (
        "parenthesized version, not a check id",
        ["hugo"],
        ["Generator held at (0.164.0) by the composite action."],
        0,
        [],
    ),
    (
        "permanent deviation, no marker and no id",
        ["hugo"],
        ["Relies on validate-task, having no get-version-task."],
        0,
        [],
    ),
    (
        "marker note on a clean audit",
        ["hugo"],
        ["The sibling doc is pending fleet ratification."],
        0,
        ["but the audit is clean"],
    ),
    # The case the old gate suppressed outright: one unclearable finding, and the note never checked.
    (
        "marker note with findings open",
        ["hugo"],
        ["The sibling doc is pending fleet ratification."],
        1,
        ["while 1 finding(s) are open"],
    ),
]

CONFORMANT = (
    "# Fixture\n\nA fixture repository.\n\n## Build and Distribution\n\n- **Source Code**: [GitHub][gh]\n\n"
    "### Build Status\n\n[![Release Status][a]][x]\\\n[![Last Commit][b]][x]\n\n"
    "### Releases\n\n[![GitHub Release][c]][x]\\\n[![GitHub Pre-Release][d]][x]\n\n"
    "### Release Notes\n\n**Version**: 1.0\n\n## Table of Contents\n\n- [Overview](#overview)\n\n"
    "## Overview\n\nWhat it does.\n\n## Whatever This Repo Calls It\n\nRepo-specific.\n\n"
    "## Questions or Issues\n\nOpen an issue.\n\n## 3rd Party Tools\n\n- [Thing][t]\n\n"
    "## License\n\nLicensed under the [MIT License][license]\\\n![License][license-shield]\n\n"
    "<!-- Shields -->\n\n"
    "[a]: https://img.shields.io/github/actions/workflow/status/o/r/publish-release.yml?label=Releases%20Build\n"
    "[b]: https://img.shields.io/github/last-commit/o/r\n"
    "[c]: https://img.shields.io/github/v/release/o/r?label=GitHub%20Release\n"
    "[d]: https://img.shields.io/github/v/release/o/r?include_prereleases&filter=*-g*&label=GitHub%20Pre-Release\n"
    "[license-shield]: https://img.shields.io/github/license/o/r\n"
)

# The four repos that suffix every heading for the ToC extension must read identically to the plain form.
OMIT_TOC = re.sub(r"^(#{2,3} .*)$", r"\1 <!-- omit from toc -->", CONFORMANT, flags=re.MULTILINE)

README_CASES = [
    ("a conformant README, repo-specific section included", CONFORMANT, set(), True, 0),
    (
        "a second intro paragraph is not a finding",
        CONFORMANT.replace(
            "A fixture repository.\n",
            "A fixture repository.\n\nAnd a clarifying paragraph about it.\n",
        ),
        set(),
        True,
        0,
    ),
    (
        "a retired section name reports a rename",
        CONFORMANT.replace("## Overview", "## Features"),
        set(),
        True,
        1,
    ),
    (
        "Questions or Issues is optional while private",
        CONFORMANT.replace("## Questions or Issues\n\nOpen an issue.\n\n", ""),
        set(),
        False,
        0,
    ),
    (
        "Questions or Issues is required when public",
        CONFORMANT.replace("## Questions or Issues\n\nOpen an issue.\n\n", ""),
        set(),
        True,
        1,
    ),
    (
        "3rd Party Tools is required in every repo",
        CONFORMANT.replace("## 3rd Party Tools\n\n- [Thing][t]\n\n", ""),
        set(),
        True,
        1,
    ),
    (
        "a Table of Contents is required with no size threshold",
        CONFORMANT.replace("## Table of Contents\n\n- [Overview](#overview)\n\n", ""),
        set(),
        True,
        1,
    ),
    (
        "an out-of-order declared section reports once",
        CONFORMANT.replace("## Questions or Issues\n\nOpen an issue.\n\n", "").replace(
            "## Overview", "## Questions or Issues\n\nOpen an issue.\n\n## Overview"
        ),
        set(),
        True,
        1,
    ),
    (
        "License must be the last section",
        CONFORMANT + "\n## TODO\n\nA backlog.\n",
        set(),
        True,
        1,
    ),
    (
        "a missing Release Notes sub-section reports once",
        CONFORMANT.replace("### Release Notes\n\n**Version**: 1.0\n\n", ""),
        set(),
        True,
        1,
    ),
    (
        "a `## ` line inside a fence is not a section",
        CONFORMANT.replace("What it does.", "```md\n## Not A Section\n```"),
        set(),
        True,
        0,
    ),
    ("Usage and Installation are N/A for source-only", CONFORMANT, {"source-only"}, True, 0),
    ("a ToC-omit comment on every heading changes nothing", OMIT_TOC, set(), True, 0),
]

DOCKER_SHIELD = "[![Docker Latest][e]][x]\n"
DOCKER_DEFINITION = "[e]: https://img.shields.io/docker/v/o/r/latest\n"

INLINE_ALL = re.sub(
    r"!\[([^\]]*)\]\[([a-z-]+)\]",
    lambda m: f"![{m.group(1)}](https://img.shields.io/PLACEHOLDER-{m.group(2)})",
    CONFORMANT,
)

SHIELD_CASES = [
    ("base shields only, a repo publishing nothing", CONFORMANT, {}, 0),
    ("a ToC-omit comment on every heading changes nothing", OMIT_TOC, {}, 0),
    ("a docker repo owes a version shield", CONFORMANT, {"publish": [{"target": "docker"}]}, 1),
    (
        "a docker repo carrying one is satisfied, whatever its channel",
        CONFORMANT.replace(
            "[![GitHub Pre-Release][d]][x]\n",
            "[![GitHub Pre-Release][d]][x]\\\n" + DOCKER_SHIELD,
        ).replace("[license-shield]:", DOCKER_DEFINITION + "[license-shield]:"),
        {"publish": [{"target": "docker"}]},
        0,
    ),
    (
        "a nuget repo owes one version shield, no prerelease",
        CONFORMANT,
        {"publish": [{"target": "nuget"}]},
        1,
    ),
    (
        "a pypi repo owes one version shield, no prerelease",
        CONFORMANT,
        {"publish": [{"target": "pypi"}]},
        1,
    ),
    (
        "a caption the repo spells differently is not a finding",
        CONFORMANT.replace("![Release Status]", "![Lint Build]"),
        {},
        0,
    ),
    (
        "an extra shield beyond the class is not a finding",
        CONFORMANT.replace(
            "[![Last Commit][b]][x]", "[![Last Commit][b]][x]\\\n[![Last Build][a]][x]"
        ),
        {},
        0,
    ),
    (
        "a shield in the wrong sub-section does not count as present",
        CONFORMANT.replace("[![GitHub Release][c]][x]\\\n", ""),
        {},
        1,
    ),
    # A badge shown as a code sample is markup, so it satisfies nothing and trips nothing.
    # Every region the README checks read comes through readme_region, which strips fences at the root.
    (
        "a fenced badge sample does not satisfy a required shield",
        CONFORMANT.replace(
            "[![GitHub Release][c]][x]\\\n", "```md\n[![GitHub Release][c]][x]\n```\n"
        ),
        {},
        1,
    ),
    (
        "a fenced license shield does not trip the exclusive rule",
        CONFORMANT.replace("## Overview", "```md\n![License][license-shield]\n```\n\n## Overview"),
        {},
        0,
    ),
    (
        "a retired badge service is reported wherever it sits",
        CONFORMANT.replace(
            "[license-shield]: https://img.shields.io/github/license/o/r\n",
            "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
        ),
        {},
        1,
    ),
    # The case above named every placement and read only the reference block, so an inline badge was invisible rather than wrong.
    # That is the reading shield_endpoints already takes for every other shield, and this one had not taken it.
    (
        "a retired badge written inline is reported",
        CONFORMANT.replace(
            "[![Last Commit][b]][x]",
            "[![Last Commit][b]][x]\\\n![Last Build](https://byob.yarr.is/o/r/lastbuild)",
        ),
        {},
        1,
    ),
    (
        "a retired badge defined and rendered is one finding, not two",
        CONFORMANT.replace(
            "[![Last Commit][b]][x]",
            "[![Last Commit][b]][x]\\\n![Last Build][last-build-shield]",
        ).replace(
            "[license-shield]: https://img.shields.io/github/license/o/r\n",
            "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
        ),
        {},
        1,
    ),
    (
        "a retired badge shown as a fenced sample is markup",
        CONFORMANT.replace(
            "## Overview",
            "```md\n![Last Build](https://byob.yarr.is/o/r/lastbuild)\n```\n\n## Overview",
        ),
        {},
        0,
    ),
    # The wording follows which of the three shapes it is, since a definition nothing renders is not rendering anything.
    # Saying it renders sends the reader looking for a badge that is not on the page.
    (
        "an unrendered definition says so rather than claiming a render",
        CONFORMANT.replace(
            "[license-shield]: https://img.shields.io/github/license/o/r\n",
            "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
        ),
        {},
        1,
        "nothing renders it",
    ),
    (
        "a rendered definition says renders",
        CONFORMANT.replace(
            "[![Last Commit][b]][x]",
            "[![Last Commit][b]][x]\\\n![Last Build][last-build-shield]",
        ).replace(
            "[license-shield]: https://img.shields.io/github/license/o/r\n",
            "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
        ),
        {},
        1,
        "renders the byob",
    ),
    # The same endpoint rendered inline leaves this definition unused, so attributing by URL would credit the render to a reference nothing uses.
    (
        "an unused definition beside an inline render is not credited with it",
        CONFORMANT.replace(
            "[![Last Commit][b]][x]",
            "[![Last Commit][b]][x]\\\n![Last Build](https://byob.yarr.is/o/r/lastbuild)",
        ).replace(
            "[license-shield]: https://img.shields.io/github/license/o/r\n",
            "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
        ),
        {},
        1,
        "rendered elsewhere",
    ),
    (
        "the pre-release shield is told from the release shield by its query",
        CONFORMANT.replace(
            "?include_prereleases&filter=*-g*&label=GitHub%20Pre-Release",
            "?label=Another%20Release",
        ),
        {},
        1,
    ),
    (
        "a pre-release shield without the prerelease filter is a finding",
        CONFORMANT.replace("&filter=*-g*&", "&"),
        {},
        1,
        "with `filter=*-g*`",
    ),
    (
        "a missing release shield names the query it forbids",
        CONFORMANT.replace("[![GitHub Release][c]][x]\\\n", ""),
        {},
        1,
        "without `include_prereleases`",
    ),
    # The license shield is an ordinary member of the base class, addressed to a different section.
    ("the license shield in the closing License section", CONFORMANT, {}, 0),
    (
        "no license shield at all",
        CONFORMANT.replace("\\\n![License][license-shield]", ""),
        {},
        1,
    ),
    (
        "the license shield left under Releases",
        CONFORMANT.replace("\\\n![License][license-shield]", "").replace(
            "[![GitHub Pre-Release][d]][x]",
            "[![GitHub Pre-Release][d]][x]\\\n![License][license-shield]",
        ),
        {},
        2,
    ),
    (
        "a shield's own reference definition is not a use of it",
        CONFORMANT.replace("![License][license-shield]\n", ""),
        {},
        1,
    ),
    # An inline shield is resolved rather than silently invisible.
    # Reading references alone let a repo writing every badge inline pass by carrying nothing the check could see.
    (
        "inline shields count as present",
        INLINE_ALL.replace("PLACEHOLDER-a", "github/actions/workflow/status/o/r")
        .replace("PLACEHOLDER-b", "github/last-commit/o/r")
        .replace("PLACEHOLDER-c", "github/v/release/o/r")
        .replace("PLACEHOLDER-d", "github/v/release/o/r?include_prereleases&filter=*-g*")
        .replace("PLACEHOLDER-license-shield", "github/license/o/r"),
        {},
        0,
    ),
    (
        "an inline shield in the wrong section is still exclusive",
        CONFORMANT.replace(
            "## Overview",
            "![License](https://img.shields.io/github/license/o/r)\n\n## Overview",
        ),
        {},
        1,
    ),
]

LINKS_OK = (
    "# F\n\nA fixture.\n\n## X\n\n[a](#x) [b][gh] [c][docker-hub-link] [d][agents] [e][upstream-link]\n\n"
    "![License][license-shield]\n\n"
    "<!-- Sections -->\n\n[x-anchor]: #x\n\n<!-- Shields -->\n\n[license-shield]: https://img.shields.io/github/license/o/r\n\n"
    "<!-- Distribution -->\n\n[actions-link]: https://github.com/o/r/actions\n[docker-hub-link]: https://hub.docker.com/r/o/r\n[github-link]: https://github.com/o/r\n\n"
    "<!-- Repo -->\n\n[agents]: ./AGENTS.md\n[license]: ./LICENSE\n\n"
    "<!-- External -->\n\n[upstream-link]: https://github.com/someone-else/their-repo\n"
)

# Kept in reference-name order, so the case measures the per-target rule rather than tripping the sort check.
TWO_IMAGES = LINKS_OK.replace(
    "[github-link]: https://github.com/o/r\n",
    "[github-link]: https://github.com/o/r\n[lsio-docker-hub-link]: https://hub.docker.com/r/o/r-lsio\n",
)

SWAPPED = LINKS_OK.replace(
    "<!-- Sections -->\n\n[x-anchor]: #x\n\n<!-- Shields -->\n\n[license-shield]: https://img.shields.io/github/license/o/r\n",
    "<!-- Shields -->\n\n[license-shield]: https://img.shields.io/github/license/o/r\n\n<!-- Sections -->\n\n[x-anchor]: #x\n",
)

LINK_CASES = [
    ("a conformant reference block", LINKS_OK, 0, 0),
    (
        "a definition-shaped line inside a paragraph is text rather than a reference",
        LINKS_OK.replace("A fixture.\n", "A fixture.\n[upstream]: https://example.com/x\n"),
        0,
        0,
    ),
    (
        "a shield reference not ending -shield",
        LINKS_OK.replace("[license-shield]", "[licence]"),
        1,
        0,
    ),
    (
        "a URI reference not ending -link",
        LINKS_OK.replace("[upstream-link]", "[upstream]"),
        1,
        0,
    ),
    (
        "a repo-local reference ending -link",
        LINKS_OK.replace("[agents]:", "[agents-link]:"),
        1,
        0,
    ),
    (
        "the repo root named for the project",
        LINKS_OK.replace("[github-link]", "[fixture-link]"),
        1,
        0,
    ),
    ("somebody else's GitHub repo is not renamed", LINKS_OK, 0, 0),
    ("one Docker Hub image takes the bare name", LINKS_OK, 0, 0),
    ("two Docker Hub images each take a target prefix", TWO_IMAGES, 1, 0),
    (
        "two Docker Hub images, both prefixed, is clean",
        TWO_IMAGES.replace(
            "[docker-hub-link]: https://hub.docker.com/r/o/r\n",
            "[base-docker-hub-link]: https://hub.docker.com/r/o/r\n",
        ),
        0,
        0,
    ),
    (
        "an undeclared group header",
        LINKS_OK.replace("<!-- External -->", "<!-- Other links -->"),
        0,
        1,
    ),
    ("groups out of the declared order", SWAPPED, 0, 1),
    (
        "a group not sorted by reference name",
        LINKS_OK.replace(
            "[agents]: ./AGENTS.md\n[license]: ./LICENSE\n",
            "[license]: ./LICENSE\n[agents]: ./AGENTS.md\n",
        ),
        0,
        1,
    ),
    (
        "a reference in the wrong group",
        LINKS_OK.replace("[license]: ./LICENSE\n", "").replace(
            "<!-- External -->\n", "<!-- External -->\n\n[license]: ./LICENSE\n"
        ),
        0,
        1,
    ),
    # A shields.io URL the document never renders is judged by that usage rather than by its host, so it is an ordinary URI.
    # Across the fleet every one of the 119 img.shields.io definitions is rendered, so a host test decides nothing.
    (
        "an unrendered shields.io reference is a URI, not a shield",
        LINKS_OK.replace(
            "[license-shield]: https://img.shields.io/github/license/o/r\n",
            "[extra-shield]: https://img.shields.io/badge/never-rendered-blue\n[license-shield]: https://img.shields.io/github/license/o/r\n",
        ),
        1,
        1,
    ),
    # A code sample showing badge markup is markup rather than a badge.
    # Reading a fenced block would invent a group header, two definitions, and a rendered shield the document does not have.
    (
        "a fenced code sample is not read as definitions",
        LINKS_OK
        + "\n## Sample\n\n```md\n<!-- Other links -->\n\n[wrong-name]: https://example.test/\n![License][license-shield]\n```\n",
        0,
        0,
    ),
    # A dependency hosted where this project also publishes stays external, so it keeps its own name.
    # The owner-scoped prefixes are what separate the two, and they live in the model rather than in the code.
    (
        "somebody else's NuGet package is not renamed nuget-link",
        LINKS_OK.replace(
            "[upstream-link]: https://github.com/someone-else/their-repo",
            "[upstream-link]: https://www.nuget.org/packages/Serilog/",
        ),
        0,
        0,
    ),
    (
        "this project's own NuGet package is renamed",
        LINKS_OK.replace(
            "[upstream-link]: https://github.com/someone-else/their-repo",
            "[upstream-link]: https://www.nuget.org/packages/o.Widget/",
        ),
        1,
        1,
    ),
    # A comment that opens no group is not a group header.
    # A markdownlint directive sits on its own line exactly like one, and reading it as a group reports a header the document never declared.
    (
        "a directive comment is not a link group",
        "<!-- markdownlint-disable MD033 -->\n" + LINKS_OK,
        0,
        0,
    ),
    # Also when it sits inside the reference block, where definitions do fall under it.
    (
        "a directive inside the reference block is not a link group",
        LINKS_OK.replace(
            "[agents]: ./AGENTS.md",
            "<!-- markdownlint-disable MD034 -->\n[agents]: ./AGENTS.md",
        ),
        0,
        0,
    ),
    # A scheme makes a target a link rather than a file, so it is named `-link` and grouped External.
    # Reading `mailto:` as a repo path would demand a bare name and the Repo group for a contact address.
    (
        "a mailto target is a URI, not a repo path",
        LINKS_OK.replace(
            "[upstream-link]: https://github.com/someone-else/their-repo",
            "[upstream-link]: mailto:someone@example.test",
        ),
        0,
        0,
    ),
    (
        "a mailto target named as a repo path is reported",
        LINKS_OK.replace(
            "[upstream-link]: https://github.com/someone-else/their-repo",
            "[upstream]: mailto:someone@example.test",
        ),
        1,
        0,
    ),
    # Two names may point at one URL, and only the rendered one is a shield.
    # Keying on the URL instead would make the plain link a shield because its twin is rendered.
    (
        "a second reference to a rendered URL is not itself a shield",
        LINKS_OK.replace(
            "[upstream-link]: https://github.com/someone-else/their-repo",
            "[upstream-link]: https://img.shields.io/github/license/o/r",
        ),
        0,
        0,
    ),
]

TOOLS_HEAD = "# F\n\nA fixture.\n\n## 3rd Party Tools\n\n"
TOOLS_DEFS = "\n[cspell-link]: https://cspell.org\n[widget-link]: https://widget.example/\n"

TOOL_CASES = [
    (
        "a catalog tool matching the catalog",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
        + TOOLS_DEFS,
        0,
    ),
    (
        "a catalog tool described differently",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell-checks README.md in CI. |\n"
        + TOOLS_DEFS,
        1,
    ),
    (
        "a catalog tool with no description at all",
        TOOLS_HEAD + "- [cspell][cspell-link]\n" + TOOLS_DEFS,
        1,
    ),
    (
        "a catalog tool linked by another URL",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][widget-link] | Spell checker. |\n"
        + TOOLS_DEFS,
        1,
    ),
    (
        "a tool the catalog does not name is not judged",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [Widget][widget-link] | whatever this repo calls it |\n"
        + TOOLS_DEFS,
        0,
    ),
    (
        "the bullet form is read like the table form",
        TOOLS_HEAD + "- [cspell][cspell-link] - Spell checker.\n" + TOOLS_DEFS,
        0,
    ),
    (
        "no section yields nothing, since its absence is already reported",
        "# F\n\nA fixture.\n\n## Other\n\nx\n",
        0,
    ),
    # A definition inside a code sample is markup being shown, so it must not resolve a tool link.
    # The sample sits before the real definition deliberately, since the first definition of a label wins.
    (
        "a fenced sample does not supply a tool link",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
        + "\n```md\n[cspell-link]: https://wrong.example/\n```\n"
        + TOOLS_DEFS,
        0,
    ),
    # A tool table shown as a sample inside the section is markup, not entries.
    (
        "a fenced tool table is not read as entries",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n\n```md\n| [cspell][cspell-link] | wrong description |\n```\n"
        + TOOLS_DEFS,
        0,
    ),
    # The list is alphabetized, which spec/readme-structure.md states and nothing checked until now.
    (
        "an unsorted tool list is reported",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [markdownlint-cli2][md-link] | Markdown linter. |\n| [cspell][cspell-link] | Spell checker. |\n"
        + TOOLS_DEFS
        + "[md-link]: https://github.com/DavidAnson/markdownlint-cli2\n",
        1,
    ),
    (
        "a sorted tool list is not",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n| [markdownlint-cli2][md-link] | Markdown linter. |\n"
        + TOOLS_DEFS
        + "[md-link]: https://github.com/DavidAnson/markdownlint-cli2\n",
        0,
    ),
    # GitHub's Markdown makes both outer pipes optional, so a row written without either is an entry rather than a non-row.
    # Requiring them read such a table as empty, which scores as clean rather than as unread.
    (
        "a row with no trailing pipe is read",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker.\n"
        + TOOLS_DEFS,
        0,
    ),
    (
        "a row with no trailing pipe is judged like any other",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell-checks the README in CI.\n"
        + TOOLS_DEFS,
        1,
    ),
    (
        "a whole table with no trailing pipes is ordered",
        TOOLS_HEAD
        + "| Tool | Role\n| --- | ---\n| [markdownlint-cli2][md-link] | Markdown linter.\n| [cspell][cspell-link] | Spell checker.\n"
        + TOOLS_DEFS
        + "[md-link]: https://github.com/DavidAnson/markdownlint-cli2\n",
        1,
    ),
    (
        "a row with no leading pipe is read",
        TOOLS_HEAD
        + "Tool | Role |\n--- | --- |\n[cspell][cspell-link] | Spell checker. |\n"
        + TOOLS_DEFS,
        0,
    ),
    (
        "a table with neither outer pipe is judged",
        TOOLS_HEAD
        + "Tool | Role\n--- | ---\n[cspell][cspell-link] | Spell-checks the README in CI.\n"
        + TOOLS_DEFS,
        1,
    ),
    # The License column is forbidden by spec/readme-structure.md, and project-types.json asserted that as `letter` before anything read it.
    # One finding is raised for the table, read off its header, rather than one per row.
    (
        "a License column is reported once",
        TOOLS_HEAD
        + "| Tool | Role | License |\n| --- | --- | --- |\n| [cspell][cspell-link] | Spell checker. | MIT |\n| [markdownlint-cli2][md-link] | Markdown linter. | MIT |\n"
        + TOOLS_DEFS
        + "[md-link]: https://github.com/DavidAnson/markdownlint-cli2\n",
        1,
    ),
    (
        "a License column does not stop the rows being read",
        TOOLS_HEAD
        + "| Tool | Role | License |\n| --- | --- | --- |\n| [cspell][cspell-link] | Spell-checks the README in CI. | MIT |\n"
        + TOOLS_DEFS,
        2,
    ),
    (
        "a table with no License column is not reported",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
        + TOOLS_DEFS,
        0,
    ),
    # A tool whose own role is the word license must not read as the column, since the header is a header.
    (
        "a row cell reading License is not the column",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [Widget][widget-link] | License |\n"
        + TOOLS_DEFS,
        0,
    ),
    (
        "a License column with no outer pipes is reported",
        TOOLS_HEAD
        + "Tool | Role | License\n--- | --- | ---\n[cspell][cspell-link] | Spell checker. | MIT\n"
        + TOOLS_DEFS,
        1,
    ),
    # A thematic break carries no pipe, so it is not a one-cell delimiter row and the paragraph above it is not a header.
    (
        "a thematic break is not a delimiter row",
        TOOLS_HEAD
        + "Licensing notes follow.\n\n---\n\n| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
        + TOOLS_DEFS,
        0,
    ),
    # Prose mentioning a tool is not a row, since a link alone does not make one.
    (
        "prose naming a cataloged tool is not a row",
        TOOLS_HEAD + "We run [cspell][cspell-link] in CI.\n" + TOOLS_DEFS,
        0,
    ),
]

DESC_README = {"README.md": "# Fixture\n\nA short tagline.\n"}

DESC_CASES = [
    (
        "no declared field, About matches the README tagline",
        DESC_README,
        {},
        {"description": "A short tagline."},
        0,
    ),
    (
        "no declared field, About mismatches the README tagline",
        DESC_README,
        {},
        {"description": "Something else."},
        1,
    ),
    (
        "declared field matches both README and About",
        DESC_README,
        {"description": "A short tagline."},
        {"description": "A short tagline."},
        0,
    ),
    (
        "declared field present, README tagline diverges from it",
        DESC_README,
        {"description": "The declared description."},
        {"description": "The declared description."},
        1,
    ),
    (
        "declared field present, About diverges from it",
        DESC_README,
        {"description": "A short tagline."},
        {"description": "Something else."},
        1,
    ),
    (
        "declared field present, no README at all, About diverges from it",
        {},
        {"description": "The declared description."},
        {"description": "Something else."},
        1,
    ),
    (
        "declared field present, no README at all, About already matches it",
        {},
        {"description": "The declared description."},
        {"description": "The declared description."},
        0,
    ),
    (
        "no declared field and no README, nothing to measure against",
        {},
        {},
        {"description": "Anything."},
        0,
    ),
    (
        "a non-string declared field is reported rather than crashing",
        DESC_README,
        {"description": 42},
        {"description": "A short tagline."},
        1,
    ),
    (
        "an explicit null declared field is reported rather than read as absent",
        DESC_README,
        {"description": None},
        {"description": "A short tagline."},
        1,
    ),
    (
        "a whitespace-only declared field is reported rather than read as absent",
        DESC_README,
        {"description": "   "},
        {"description": "A short tagline."},
        1,
    ),
    (
        "a padded declared field is a DEFECT rather than silently trimmed",
        DESC_README,
        {"description": "  A short tagline.  "},
        {"description": "A short tagline."},
        1,
    ),
    (
        "a declared field with an embedded newline is a DEFECT rather than silently accepted",
        DESC_README,
        {"description": "A short tagline.\nA second line."},
        {"description": "A short tagline."},
        1,
    ),
    (
        "a declared field carrying a Markdown link is a DEFECT rather than silently canonical",
        DESC_README,
        {"description": "See [docs](https://example.test) for more."},
        {"description": "A short tagline."},
        1,
    ),
    (
        "a declared field over the 100-char cap is a DEFECT rather than silently canonical",
        DESC_README,
        {"description": "a" * 101},
        {"description": "A short tagline."},
        1,
    ),
]


def holding(*paths: str) -> Callable[..., Any]:
    def fake(path: str, ok404: bool = False) -> Any:
        if "/commits?path=" in path:
            name = path.split("/commits?path=", 1)[1].split("&", 1)[0]
            stamp = {"commit": {"committer": {"date": "2000-01-01T00:00:00+00:00"}}}
            return [stamp] if name in paths else []
        name = path.split("/contents/", 1)[1].split("?", 1)[0]
        return {"encoding": "base64", "content": "eA==", "name": name} if name in paths else None

    return fake


BASELINE_UNIT = {"path": ".codecov.yml", "fidelity": "intent", "intentRef": "WORKFLOW.md"}


class DriftnoteCase(unittest.TestCase):
    """The driftNote freshness rules, driven directly rather than through audit_repo."""

    def test_note_cases(self) -> None:
        for label, types, notes, open_count, wanted in NOTE_CASES:
            with self.subTest(label):
                got = audit.driftnote_findings(
                    {"types": types, "driftNotes": notes}, NOTE_SPEC, open_count
                )
                texts = [t for _, t in got]
                self.assertTrue(
                    len(texts) == len(wanted) and all(w in t for w, t in zip(wanted, texts)),
                    f"driftNote {label} -> {texts}, want {wanted}",
                )

    def test_missing_catalog_raises_and_names_the_file(self) -> None:
        with self.assertRaises(KeyError, msg="missing catalog: no error raised") as ctx:
            audit.driftnote_findings(
                {"types": ["hugo"], "driftNotes": ["A note (hugo.build.strict)."]},
                {"registry": {}},
                0,
            )
        self.assertIn(
            "project-types.json",
            str(ctx.exception),
            f"missing catalog: error does not name the file -> {ctx.exception}",
        )


class ReadmeCase(unittest.TestCase):
    """The README structure engine, run against the real declared model."""

    def setUp(self) -> None:
        self.rm = audit.load("spec/readme-sections.json")

    def test_section_cases(self) -> None:
        for label, text, sel_types, public, wantn in README_CASES:
            with self.subTest(label):
                got = audit_markdown.readme_section_findings(text, self.rm, sel_types, public)
                self.assertEqual(len(got), wantn, f"readme sections: {label} -> {got}")

    def test_shield_cases(self) -> None:
        for case in SHIELD_CASES:
            label, text, ent, wantn = case[:4]
            want_text = case[4] if len(case) > 4 else None
            with self.subTest(label):
                got = audit_markdown.readme_shield_findings(text, self.rm, ent)
                good = len(got) == wantn and (
                    want_text is None or any(want_text in t for _, t in got)
                )
                self.assertTrue(good, f"readme shields: {label} want={wantn} {want_text} -> {got}")

    def test_link_cases(self) -> None:
        for label, text, want_letter, want_drift in LINK_CASES:
            with self.subTest(label):
                got = audit_markdown.readme_link_findings(text, self.rm, "o/r")
                gl = sum(1 for k, _ in got if k == "LETTER")
                gd = sum(1 for k, _ in got if k == "DRIFT")
                self.assertEqual(
                    (gl, gd), (want_letter, want_drift), f"readme links: {label} {got}"
                )

    def test_tool_cases(self) -> None:
        cat = audit.load("spec/third-party-tools.json")
        for label, text, wantn in TOOL_CASES:
            with self.subTest(label):
                got = audit_markdown.third_party_tool_findings(text, cat)
                self.assertEqual(len(got), wantn, f"3rd party tools: {label} -> {got}")

    def test_canonical_link_entry_with_neither_repopath_nor_match_raises(self) -> None:
        broken = dict(
            self.rm, canonicalLinks=list(self.rm["canonicalLinks"]) + [{"name": "orphan-link"}]
        )
        with self.assertRaises(
            KeyError,
            msg="canonicalLinks entry with neither repoPath nor match: no error raised",
        ) as ctx:
            audit_markdown.canonical_link_entry("https://example.test/", broken, "o/r")
        message = str(ctx.exception)
        self.assertTrue(
            "readme-sections.json" in message and "orphan-link" in message,
            f"canonicalLinks entry error does not name the file and entry -> {ctx.exception}",
        )

    def test_a_reference_resolves_to_the_definition_github_renders(self) -> None:
        text = (
            "# X\n\n![x][Docs]\n\n[docs]: https://example.com/a\n"
            "[docs]: https://example.com/b\n[Docs]: https://example.com/c\n"
        )
        defs = audit_markdown.reference_table(text)
        self.assertEqual(
            audit_markdown.shield_endpoints("![x][Docs]", defs), ["https://example.com/a"]
        )

    def test_tagline_is_the_first_line_of_the_intro_region(self) -> None:
        two_para = audit_markdown.title_and_intro(
            "# X\n\nThe tagline.\n\nA clarifying paragraph.\n\n## Next\n"
        )[1]
        self.assertEqual(audit_markdown.tagline(two_para), "The tagline.")
        self.assertNotIn("\n", audit_markdown.tagline(two_para))


class DescriptionCase(unittest.TestCase):
    """description_findings(): the README, About, and Docker Hub mirror set."""

    def test_description_cases(self) -> None:
        for label, doc_texts_fx, entry_fx, live_fx, wantn in DESC_CASES:
            with self.subTest(label):
                got = audit_markdown.description_findings(
                    doc_texts_fx, entry_fx, live_fx, "owner/Fixture"
                )
                self.assertEqual(len(got), wantn, f"description: {label} -> {got}")

    def test_null_declared_field_is_a_defect_via_validates_contract(self) -> None:
        null_declared = audit_markdown.description_findings(
            DESC_README,
            {"description": None},
            {"description": "A short tagline."},
            "owner/Fixture",
        )
        self.assertTrue(
            any(
                k == "DEFECT" and "description must be a non-empty string" in t
                for k, t in null_declared
            ),
            f"description: null-declared-field DEFECT contract -> {null_declared}",
        )

    def test_declared_field_defect_names_the_repo(self) -> None:
        null_declared = audit_markdown.description_findings(
            DESC_README,
            {"description": None},
            {"description": "A short tagline."},
            "owner/Fixture",
        )
        self.assertTrue(
            any(k == "DEFECT" and t.startswith("owner/Fixture:") for k, t in null_declared),
            f"description: DEFECT does not name the repo -> {null_declared}",
        )

    def test_declared_field_names_the_registry_as_the_source(self) -> None:
        declared_mismatch = audit_markdown.description_findings(
            DESC_README,
            {"description": "The declared description."},
            {"description": "Something else."},
            "owner/Fixture",
        )
        self.assertTrue(
            any(
                "declared description" in t and "registry/repos.json" in t
                for _, t in declared_mismatch
            ),
            f"description: declared-field wording -> {declared_mismatch}",
        )

    def test_stale_docker_hub_short_description_is_reported(self) -> None:
        with mock.patch.object(
            audit_markdown, "docker_hub_description", lambda slug: "A stale Docker Hub blurb."
        ):
            docker_mismatch = audit_markdown.description_findings(
                DESC_README,
                {"description": "A short tagline.", "publish": [{"target": "docker"}]},
                {"description": "A short tagline."},
                "owner/Fixture",
            )
        self.assertTrue(
            any("Docker Hub short description" in t for _, t in docker_mismatch),
            f"description: Docker Hub mismatch not reported -> {docker_mismatch}",
        )


class GroundTruthCase(unittest.TestCase):
    """audit_repo, hub_only_paths, gap_dispositions, repo_tree, and baseline file reads."""

    def test_missing_ground_branch_is_one_error(self) -> None:
        entry = {"name": "Fixture", "url": "https://github.com/owner/Fixture", "hasDevelop": True}
        with mock.patch.object(
            audit,
            "gh",
            lambda path, ok404=False: None if "/branches/" in path else {"private": False},
        ):
            missing_findings, missing_sha = audit.audit_repo(
                entry, {"registry": {}}, "no-such-branch"
            )
        kinds = [k for k, _ in missing_findings]
        errors = [t for k, t in missing_findings if k == "ERROR"]
        self.assertTrue(
            missing_sha == ""
            and len(errors) == 1
            and "no-such-branch" in errors[0]
            and kinds == ["DEFECT", "DRIFT", "ERROR"],
            f"missing ground branch -> {missing_findings}",
        )

    def test_hub_only_set_excludes_every_declared_path(self) -> None:
        declared = {e["path"] for e in audit.load("spec/files.json")["baseline"]}
        hub_only = audit.hub_only_paths({"files": audit.load("spec/files.json")}, rev="HEAD")
        leaked = sorted(declared & hub_only)
        self.assertTrue(
            not leaked and hub_only,
            f"hub-only set -> {len(hub_only)} paths, {len(leaked)} declared leaked: {leaked}",
        )

    def test_gap_dispositions_drop_malformed_rows(self) -> None:
        led = {
            "divergences": {
                "gaps": [
                    {"path": "a/b.sh", "disposition": "retire", "reason": "r"},
                    {"path": "c/d.md", "disposition": "accepted"},
                    {"path": "no-disposition.md"},
                    {"disposition": "retire"},
                    "not-an-object",
                ]
            }
        }
        got_disp = audit.gap_dispositions(led)
        self.assertEqual(
            got_disp,
            {"a/b.sh": ("retire", "r"), "c/d.md": ("accepted", "")},
            f"gap disposition parsing -> {got_disp}",
        )

    def test_repo_tree_returns_none_unless_whole(self) -> None:
        head = {"commit": {"commit": {"tree": {"sha": "deadbeef"}}}}
        with mock.patch.object(
            audit,
            "gh",
            lambda path, ok404=False: {
                "tree": [{"path": "x", "type": "blob"}],
                "truncated": True,
            },
        ):
            truncated = audit.repo_tree("o/r", head)
        with mock.patch.object(
            audit,
            "gh",
            lambda path, ok404=False: {
                "tree": [{"path": "x", "type": "blob"}, {"path": "d", "type": "tree"}],
                "truncated": False,
            },
        ):
            whole = audit.repo_tree("o/r", head)
        self.assertTrue(
            truncated is None and audit.repo_tree("o/r", {"commit": {}}) is None and whole == {"x"},
            f"repo_tree -> truncated={truncated}, whole={whole}",
        )

    def test_read_baseline_file(self) -> None:
        for held, want, label in (
            ((".codecov.yml",), ".codecov.yml", "the declared path"),
            (("codecov.yml",), None, "only another name Codecov reads"),
            ((), None, "neither"),
        ):
            with self.subTest(label):
                with mock.patch.object(audit, "gh", holding(*held)):
                    got = audit.read_baseline_file("o/r", "main", ".codecov.yml")
                self.assertEqual(got["name"] if got is not None else None, want, label)

    def test_baseline_file_findings_present(self) -> None:
        with mock.patch.object(audit, "gh", holding(".codecov.yml")):
            content, text, findings = audit.baseline_file_findings(
                "o/r", "main", ".codecov.yml", BASELINE_UNIT
            )
        self.assertIsNotNone(content)
        self.assertEqual((text, findings), ("x", []))

    def test_baseline_file_findings_absent(self) -> None:
        with mock.patch.object(audit, "gh", holding("codecov.yml")):
            _, _, findings = audit.baseline_file_findings(
                "o/r", "main", ".codecov.yml", BASELINE_UNIT
            )
        self.assertEqual(
            findings,
            [
                (
                    "LETTER",
                    "file: .codecov.yml absent on main (verify intent per AUDIT.md section 7)",
                )
            ],
        )


HUB_CHANGE = ("2020-01-01T00:00:00+00:00", "abc1234")
OLD_COMMIT = [{"commit": {"committer": {"date": "2019-01-01T00:00:00+00:00"}}}]


def intent_run(baseline: dict[str, Any], content: dict[str, str], notes: list[str]) -> Any:
    """A non-hub Run holding `content` as read, for check_files_intent."""
    run = audit.Run({"name": "Widget", "driftNotes": notes}, {}, "o/r", "main", {}, None, None)
    run.head = {"commit": {"sha": "c0ffee"}}
    run.__dict__["baseline"] = {p: (item, set(), set()) for p, item in baseline.items()}
    run.content.update(content)
    return run


class IntentAdvisoryCase(unittest.TestCase):
    """The files.intent advisory: when a copy reads as trailing, and how a driftNote acknowledges it."""

    def patched(self, texts: dict[str, str], commit: Any = OLD_COMMIT) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(audit, "hub_last_change", lambda rel, anchor=None: HUB_CHANGE)
        )
        stack.enter_context(mock.patch.object(audit, "canonical_current_text", texts.get))
        stack.enter_context(mock.patch.object(audit, "gh", lambda ep, ok404=False: commit))
        return stack

    def test_a_copy_matching_the_hub_file_at_path_is_current(self) -> None:
        unit = {"path": "widget.json", "fidelity": "intent", "intentRef": "DOC.md#widget"}
        texts = {"DOC.md": "the doc", "widget.json": "same"}
        trailing = ("DOC.md", ("2019-01-01T00:00:00+00:00", *HUB_CHANGE))
        for down, want in (("same", None), ("x", trailing)):
            with self.subTest(down=down), self.patched(texts):
                got = audit.intent_trail("o/r", "main", "widget.json", unit, down)
            self.assertEqual(got, want)

    def test_a_repo_owned_key_never_reads_as_trailing(self) -> None:
        unit = {
            "path": "global.json",
            "fidelity": "intent",
            "reference": "snip/global.json",
            "repoOwnedKeys": ["sdk.version"],
        }
        canon = '{"sdk": {"version": "10.0.100", "rollForward": "latestFeature"}}'
        trailing = ("snip/global.json", ("2019-01-01T00:00:00+00:00", *HUB_CHANGE))
        cases = [
            (
                "another line",
                '{"sdk": {"version": "11.0.100", "rollForward": "latestFeature"}}',
                None,
            ),
            (
                "a key of its own",
                '{"sdk": {"version": "9.0.100", "rollForward": "latestFeature"}, "test": {"runner": "x"}}',
                None,
            ),
            (
                "a fleet value changed",
                '{"sdk": {"version": "11.0.100", "rollForward": "major"}}',
                trailing,
            ),
            ("a fleet value dropped", '{"sdk": {"version": "11.0.100"}}', trailing),
            (
                "a value the canonical dropped",
                '{"sdk": {"version": "11.0.100", "rollForward": "latestFeature", "allowPrerelease": false}}',
                trailing,
            ),
            (
                "a byte-order mark",
                '\ufeff{"sdk": {"version": "11.0.100", "rollForward": "latestFeature"}}',
                None,
            ),
            ("not JSON", "{", trailing),
            ("not an object", "[]", trailing),
        ]
        for label, down, want in cases:
            with self.subTest(label), self.patched({"snip/global.json": canon}):
                got = audit.intent_trail("o/r", "main", "global.json", unit, down)
                self.assertEqual(got, want)

    def test_a_unit_without_repo_owned_keys_compares_whole(self) -> None:
        unit = {"path": "global.json", "fidelity": "intent", "reference": "snip/global.json"}
        canon = '{"sdk": {"version": "10.0.100"}}'
        with self.patched({"snip/global.json": canon}):
            got = audit.intent_trail(
                "o/r", "main", "global.json", unit, '{"sdk": {"version": "11.0.100"}}'
            )
        self.assertIsNotNone(got)

    def test_the_trail_dates_by_the_intent_ref_anchor(self) -> None:
        asked: list[tuple[str, str | None]] = []

        def change(rel: str, anchor: str | None = None) -> tuple[str, str]:
            asked.append((rel, anchor))
            return HUB_CHANGE

        unit = {"path": "widget.json", "fidelity": "intent", "intentRef": "DOC.md#widget"}
        with (
            self.patched({"DOC.md": "the doc"}),
            mock.patch.object(audit, "hub_last_change", change),
        ):
            audit.intent_trail("o/r", "main", "widget.json", unit, "x")
        self.assertEqual(asked, [("DOC.md", "widget")])

    def test_a_reference_unit_ignores_the_hub_file_at_path(self) -> None:
        unit = {"path": "w.yml", "fidelity": "intent", "reference": "snip/w.yml"}
        with self.patched({"snip/w.yml": "snippet", "w.yml": "same"}):
            self.assertIsNotNone(audit.intent_trail("o/r", "main", "w.yml", unit, "same"))

    def test_a_copy_newer_than_the_canonical_is_current(self) -> None:
        unit = {"path": "w.json", "fidelity": "intent", "intentRef": "DOC.md"}
        newer = [{"commit": {"committer": {"date": "2021-01-01T00:00:00+00:00"}}}]
        with self.patched({"DOC.md": "canonical"}, newer):
            self.assertIsNone(audit.intent_trail("o/r", "main", "w.json", unit, "x"))

    def test_the_finding_names_the_intent_canonical(self) -> None:
        trails = {"w.json": ("DOC.md", ("2019-01-01T00:00:00+00:00", *HUB_CHANGE))}
        findings = audit.intent_findings({}, "main", trails)
        self.assertEqual(len(findings), 1)
        self.assertIn("the intent canonical DOC.md changed later at 2020-01-01", findings[0][1])

    def test_an_acknowledgement_clears_the_change_it_names_and_no_other(self) -> None:
        trails = {"w.json": ("DOC.md", ("2019-01-01T00:00:00+00:00", *HUB_CHANGE))}
        cases = [
            ("the cited sha", "Verified `w.json@abc1234`, nothing to adopt (files.intent).", 0, 0),
            ("a full sha", "Verified `w.json@abc1234" + "0" * 33 + "` (files.intent).", 0, 0),
            ("an older sha", "Verified `w.json@def5678` (files.intent).", 1, 1),
            ("another path", "Verified `x.json@abc1234` (files.intent).", 1, 1),
            ("no code span", "Verified w.json, nothing to adopt (files.intent).", 1, 1),
            ("no check id", "Verified `w.json@abc1234`.", 1, 0),
        ]
        for label, note, advisories, registry in cases:
            with self.subTest(label):
                findings = audit.intent_findings({"driftNotes": [note]}, "main", trails)
                got = [t for _, t in findings]
                self.assertEqual(sum(t.startswith("intent:") for t in got), advisories, got)
                self.assertEqual(sum(t.startswith("registry:") for t in got), registry, got)

    def test_an_acknowledgement_of_a_current_copy_is_stale(self) -> None:
        findings = audit.intent_findings(
            {"driftNotes": ["`w.json@abc1234` (files.intent)"]}, "main", {}
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("acknowledges 1 copy(ies) the advisory does not report", findings[0][1])

    def test_check_files_intent_reads_only_intent_units_it_read(self) -> None:
        intent = {"path": "w.json", "fidelity": "intent", "intentRef": "DOC.md"}
        run = intent_run(
            {"w.json": intent, "unread.json": intent, "AGENTS.md": None},
            {"w.json": "x", "AGENTS.md": "x"},
            [],
        )
        with self.patched({"DOC.md": "canonical"}):
            findings = audit.check_files_intent(run)
        self.assertEqual([t.split(" ", 2)[1] for _, t in findings], ["w.json"])

    def test_check_files_intent_keys_a_copy_by_its_dotfile_path(self) -> None:
        asked: list[str] = []

        def fake(ep: str, ok404: bool = False) -> Any:
            asked.append(ep)
            return OLD_COMMIT

        for notes, want in (
            ([], [".codecov.yml"]),
            (["`.codecov.yml@abc1234` (files.intent)"], []),
        ):
            run = intent_run({".codecov.yml": BASELINE_UNIT}, {".codecov.yml": "x"}, notes)
            asked.clear()
            with (
                self.subTest(notes=notes),
                self.patched({"WORKFLOW.md": "canonical"}),
                mock.patch.object(audit, "gh", fake),
            ):
                findings = audit.check_files_intent(run)
            self.assertEqual(asked, ["repos/o/r/commits?path=.codecov.yml&sha=c0ffee&per_page=1"])
            self.assertEqual([t.split(" ", 2)[1] for _, t in findings], want)

    def test_the_anchor_is_read_only_from_an_intent_ref(self) -> None:
        cases = [
            ({"intentRef": "GOVERNANCE.md#line-endings"}, "line-endings"),
            ({"intentRef": "GOVERNANCE.md"}, None),
            ({"intentRef": "GOVERNANCE.md#"}, None),
            ({"intentRef": "#line-endings"}, None),
            ({"reference": "snip/w.yml", "intentRef": "WORKFLOW.md#d3"}, None),
            ({}, None),
        ]
        for item, want in cases:
            with self.subTest(item=item):
                self.assertEqual(audit.intent_ref(item, "w.json")[1], want)


class AnchoredLastChangeCase(unittest.TestCase):
    """hub_last_change dates an anchored intentRef by its section, over constructed revisions."""

    def test_an_edit_outside_the_anchored_section_does_not_date_it(self) -> None:
        doc = "# Doc\n\n## Rules\n\n### Line Endings\n\n{le}\n\n### Other\n\n{other}\n"
        revisions = [
            ("2022-01-01T00:00:00+00:00", "c" * 40, doc.format(le="lf", other="three")),
            ("2021-01-01T00:00:00+00:00", "b" * 40, doc.format(le="lf", other="two")),
            ("2020-01-01T00:00:00+00:00", "a" * 40, doc.format(le="crlf", other="one")),
        ]
        cases = [
            ("the anchored section", "line-endings", ("2021-01-01T00:00:00+00:00", "bbbbbbb")),
            ("a sibling section", "other", ("2022-01-01T00:00:00+00:00", "ccccccc")),
            ("an unresolved anchor", "gone", ("2022-01-01T00:00:00+00:00", "ccccccc")),
            ("no anchor", None, ("2022-01-01T00:00:00+00:00", "ccccccc")),
        ]
        for label, anchor, want in cases:
            with self.subTest(label):
                audit.hub_last_change.cache_clear()
                self.addCleanup(audit.hub_last_change.cache_clear)
                with mock.patch.object(audit, "_git_revisions", lambda rel: revisions):
                    self.assertEqual(audit.hub_last_change("DOC.md", anchor), want)

    def test_a_section_new_in_history_dates_from_its_introduction(self) -> None:
        revisions = [
            ("2022-01-01T00:00:00+00:00", "c" * 40, "## A\n\nintro\n\n### Line Endings\n\nlf\n"),
            ("2021-01-01T00:00:00+00:00", "b" * 40, "## A\n\n### Line Endings\n\nlf\n"),
            ("2020-01-01T00:00:00+00:00", "a" * 40, "## A\n"),
        ]
        audit.hub_last_change.cache_clear()
        self.addCleanup(audit.hub_last_change.cache_clear)
        with mock.patch.object(audit, "_git_revisions", lambda rel: revisions):
            got = audit.hub_last_change("DOC.md", "line-endings")
        self.assertEqual(got, ("2021-01-01T00:00:00+00:00", "bbbbbbb"))

    def test_every_anchored_intent_ref_resolves_in_the_hub(self) -> None:
        for item in audit.load("spec/files.json")["baseline"]:
            rel, anchor = audit.intent_ref(item, item["path"])
            if anchor is None:
                continue
            with self.subTest(path=item["path"], intentRef=item["intentRef"]):
                text = (audit.ROOT / rel).read_text(encoding="utf-8")
                self.assertIsNotNone(audit.extract_anchored(text, anchor))


class CoverageTypesCase(unittest.TestCase):
    """Holds the coverage-candidate and coverage-claiming type helpers."""

    def test_coverage_candidate_types(self) -> None:
        candidate_cases = [
            (
                ["csharp", "python"],
                {"csharp": "lint-only", "python": "lint-only"},
                ["python"],
                "only Python's lint-only owes coverage",
            ),
            (["csharp"], {"csharp": "build"}, ["csharp"], "a build language stays a candidate"),
            (["csharp"], None, ["csharp"], "a malformed profiles value is read as none"),
        ]
        for types_in, profiles_in, expected, label in candidate_cases:
            with self.subTest(label):
                got = audit.coverage_candidate_types(types_in, profiles_in)
                self.assertEqual(got, expected)

    def test_coverage_claiming_types(self) -> None:
        type_mechs = {"csharp": "codecov", "python": "codecov"}
        coverage_cases = [
            (["python"], {"src/pkg/__init__.py", "pyproject.toml"}, [], "python, no tests"),
            (["python"], {"tests/test_a.py", "pyproject.toml"}, ["python"], "python with tests"),
            (
                ["python"],
                {"tests/test_a.py"},
                ["python"],
                "python lint-only, with tests, owes the claim same as build",
            ),
            (["csharp"], {"src/App/App.csproj"}, [], "csharp, no test project"),
            (["csharp"], {"test/App.Tests/App.Tests.csproj"}, ["csharp"], "csharp with tests"),
            (["csharp"], {"src/Tests/App.csproj"}, ["csharp"], "csharp, located under Tests/"),
            (
                ["csharp"],
                {"test/Specs.csproj"},
                ["csharp"],
                "csharp, located as a test, named nothing",
            ),
            (
                ["csharp"],
                {"src/App/App.csproj", "docs/x.md"},
                [],
                "csharp, neither named nor located as a test",
            ),
            (
                ["python"],
                {"test_app.py", "pyproject.toml"},
                ["python"],
                "python, root-level test module",
            ),
            (
                ["python"],
                {"app_test.py", "pyproject.toml"},
                ["python"],
                "python, trailing _test module",
            ),
            (
                ["python"],
                {"src/app.py", "contest.py"},
                [],
                "python, a module merely containing test",
            ),
            (
                ["python"],
                {"tests/README.md", "pyproject.toml"},
                [],
                "python, a test directory holding no Python",
            ),
            (
                ["python"],
                {"tests/conftest.py", "pyproject.toml"},
                ["python"],
                "python, a test directory holding Python",
            ),
            (
                ["csharp"],
                {"test/App.UnitTest.csproj"},
                ["csharp"],
                "csharp, singular Test in the name",
            ),
            (
                ["csharp"],
                {"src/widget.tests.csproj"},
                ["csharp"],
                "csharp, a lowercase test segment outside a test directory",
            ),
            (
                ["csharp"],
                {"src/Contest.csproj", "src/Latest.csproj"},
                [],
                "csharp, names carrying those letters inside a word rather than as a segment",
            ),
            (
                ["python"],
                {"test/test_a.py", "pyproject.toml"},
                ["python"],
                "python, singular test dir",
            ),
            (["python"], {"src/tests/test_a.py"}, ["python"], "python, tests below the root"),
            (["python"], {"tests"}, [], "python, a file named tests is not a directory"),
            (
                ["csharp", "python"],
                {"test/App.Tests/App.Tests.csproj", "tools/py/pyproject.toml"},
                ["csharp"],
                "mixed repo, the tested C# side claims coverage, the untested lint-only python side does not",
            ),
            (["python"], None, ["python"], "unreadable tree keeps the claim"),
            (["docker"], {"tests/test_a.py"}, [], "a type mapped to no mechanism claims nothing"),
        ]
        for types_in, tree_in, expected, label in coverage_cases:
            with self.subTest(label):
                got = audit.coverage_claiming_types(types_in, type_mechs, tree_in)
                self.assertEqual(got, expected)


class PythonTypeFindingsCase(unittest.TestCase):
    """Holds the advisory for a tracked Python file in a repo with no python type."""

    def test_python_type_findings(self) -> None:
        # Constructed repo shapes throughout, never a registry entry.
        python_type_cases = [
            (
                {"types": ["docs"]},
                {"tools/script.py"},
                1,
                "untyped python: a tracked .py file, no 'python' type",
            ),
            (
                {"types": ["python"]},
                {"tools/script.py"},
                0,
                "typed python: 'python' already declared",
            ),
            ({"types": []}, None, 0, "unreadable tree: presence is unanswerable"),
            ({"types": []}, {"README.md"}, 0, "no .py file anywhere: nothing to flag"),
            ({"types": None}, {"tools/script.py"}, 1, "a malformed types value reads as untyped"),
            (
                {"types": ["docs"]},
                {".husky/hub_fetch_run.py"},
                0,
                "the carried hook helper is not the repo's Python",
            ),
            (
                {"types": ["docs"]},
                {".husky/hub-fetch-run.py"},
                0,
                "the carried hook helper under its former name is not the repo's Python",
            ),
            (
                {
                    "types": ["docs"],
                    "driftNotes": ["Vendored tool, not tracked (python.directories.declared)."],
                },
                {"tools/script.py"},
                0,
                "a driftNote naming the check suppresses the advisory",
            ),
            (
                {
                    "types": ["docs"],
                    "driftNotes": ["Retired helper (python.directories.declared)."],
                },
                {"README.md"},
                1,
                "a suppression with no .py file left is stale",
            ),
            (
                {
                    "types": ["docs"],
                    "driftNotes": ["python.directories.declared without parentheses"],
                },
                {"tools/script.py"},
                1,
                "only the parenthesized id suppresses",
            ),
        ]
        for entry_in, tree_in, expected, label in python_type_cases:
            with self.subTest(label):
                got = len(audit.python_type_findings(entry_in, tree_in))
                self.assertEqual(got, expected)


class FileNamingCase(unittest.TestCase):
    """Holds the file naming findings, their driftNote exemptions, and the capped listing."""

    def test_file_naming_cases(self) -> None:
        plain = "100644"
        exe = "100755"
        file_naming_cases: list[tuple[Any, ...]] = [
            (
                "every conforming shape passes",
                {
                    "scripts/fetch_report.py": plain,
                    "pkg/__init__.py": plain,
                    "tests/conftest.py": plain,
                    "scripts/sync-tools.sh": exe,
                    "scripts/sync-tools.ps1": plain,
                    "scripts/sync-tools.cmd": plain,
                    "scripts/sync-tools.bat": plain,
                    "tests/sync-tools.Tests.ps1": plain,
                    ".husky/pre-commit": exe,
                    "bin/run-backup": exe,
                    "PSScriptAnalyzerSettings.psd1": plain,
                    "Makefile": plain,
                    "README.md": plain,
                    ".envrc": exe,
                },
                {},
                set(),
                set(),
            ),
            (
                "a hyphenated or capitalized .py breaks",
                {
                    "scripts/fetch-report.py": plain,
                    "scripts/FetchReport.py": plain,
                    "tool.PY": plain,
                },
                {},
                {"scripts/fetch-report.py", "scripts/FetchReport.py", "tool.PY"},
                set(),
            ),
            (
                "the carried hook helper under its former name breaks, beside the current name or alone",
                {".husky/hub-fetch-run.py": plain, ".husky/hub_fetch_run.py": plain},
                {},
                {".husky/hub-fetch-run.py"},
                set(),
            ),
            (
                "a snake_case or capitalized shell, PowerShell, or batch name with no wrapped tool breaks",
                {
                    "scripts/sync_tools.sh": exe,
                    "scripts/Sync-Tools.ps1": plain,
                    "scripts/SYNC.CMD": plain,
                    "scripts/sync-tools.SH": exe,
                    "scripts/sync_tools.bat": plain,
                },
                {},
                {
                    "scripts/sync_tools.sh",
                    "scripts/Sync-Tools.ps1",
                    "scripts/SYNC.CMD",
                    "scripts/sync-tools.SH",
                    "scripts/sync_tools.bat",
                },
                set(),
            ),
            (
                "a Pester suffix over a non-conforming stem breaks",
                {"tests/Sync_Tools.Tests.ps1": plain},
                {},
                {"tests/Sync_Tools.Tests.ps1"},
                set(),
            ),
            (
                "a launcher beside its Python tool keeps the tool's stem",
                {
                    "scripts/fetch_report.py": plain,
                    "scripts/fetch_report.sh": exe,
                    "scripts/fetch_report.ps1": plain,
                    "scripts/fetch_report.cmd": plain,
                },
                {},
                set(),
                {},
            ),
            (
                "a launcher whose Python tool sits in another directory is undecided",
                {"tools/fetch_report.py": plain, "scripts/fetch_report.sh": exe},
                {},
                set(),
                {"scripts/fetch_report.sh"},
            ),
            (
                "an extensionless launcher beside its Python tool may be an installed command, with no read",
                {"scripts/fetch_report.py": plain, "scripts/fetch_report": exe},
                {},
                set(),
                {"scripts/fetch_report"},
            ),
            (
                "an extensionless launcher whose Python tool sits elsewhere is undecided, with no read",
                {"tools/fetch_report.py": plain, "bin/fetch_report": exe},
                {},
                set(),
                {"bin/fetch_report"},
            ),
            (
                "an extensionless executable with a shebang and a non-conforming name breaks",
                {"bin/run_backup": exe, "bin/RunBackup": exe},
                {"bin/run_backup": "#!/usr/bin/env bash\n", "bin/RunBackup": "#!/bin/sh\n"},
                {"bin/run_backup", "bin/RunBackup"},
                set(),
            ),
            (
                "an extensionless executable with no shebang, or no readable text, is undecided",
                {"bin/Tool_Binary": exe, "bin/Unreadable_Tool": exe},
                {"bin/Tool_Binary": "\x7fELF", "bin/Unreadable_Tool": None},
                set(),
                {"bin/Tool_Binary", "bin/Unreadable_Tool"},
            ),
            (
                "an extensionless file without the executable bit is out of scope",
                {"docs/Release_Notes": plain},
                {},
                set(),
                set(),
            ),
            (
                "an executable dotfile and a dotted name are not judged, with no read",
                {"bin/.Hidden_Hook": exe, "bin/Run_Me.v2": exe},
                {},
                set(),
                set(),
            ),
            (
                "a dotfile with a listed extension is judged on its stem",
                {"bin/.Hook_Run.sh": exe, "bin/.lint.py": plain},
                {},
                {"bin/.Hook_Run.sh", "bin/.lint.py"},
                set(),
            ),
        ]
        for label, blobs_in, texts_in, want_broken, want_undecided in file_naming_cases:
            with self.subTest(label):
                reads: list[str] = []

                def read_text(path, texts_in=texts_in, reads=reads):
                    reads.append(path)
                    return texts_in.get(path)

                got = audit.file_naming_findings({}, blobs_in, read_text)
                broken_text = next((t for _, t in got if " break " in t), "")
                undecided_text = next((t for _, t in got if "cannot decide" in t), "")
                good = (
                    all(kind == "DRIFT" for kind, _ in got)
                    and len(got) == bool(want_broken) + bool(want_undecided)
                    and broken_text.startswith(
                        f"file-naming: {len(want_broken)} file(s)" if want_broken else ""
                    )
                    and all(
                        f"{p}," in f"{broken_text}," or f"{p} " in broken_text for p in want_broken
                    )
                    and undecided_text.startswith(
                        f"file-naming: {len(want_undecided)} file(s)" if want_undecided else ""
                    )
                    and all(f"{p} (" in undecided_text for p in want_undecided)
                    and set(reads) == set(texts_in)
                )
                self.assertTrue(good, f"{got}, reads {reads}")

    def test_file_naming_capped(self) -> None:
        plain = "100644"
        many = {f"scripts/Tool{n}.py": plain for n in range(7)}
        capped = audit.file_naming_findings({}, many, lambda path: None)
        self.assertTrue(
            len(capped) == 1 and capped[0][1].endswith(" and 2 more"),
            f"past five paths the list is capped -> {capped}",
        )

    def test_file_naming_exemptions(self) -> None:
        plain = "100644"
        exe = "100755"
        exempt_note = (
            "A package format fixes `tools/Install.ps1` by that name (filenaming.scripts)."
        )
        exemption_blobs = {
            "tools/Install.ps1": plain,
            "tools/Setup_Host.sh": exe,
            "bin/Tool_Binary": exe,
        }
        exemption_cases = [
            (
                "a named path is exempt and an unnamed break still reports",
                [exempt_note],
                exemption_blobs,
                {"tools/Setup_Host.sh"},
                {"bin/Tool_Binary"},
                [],
            ),
            (
                "an undecided path can be exempted too",
                [
                    "An installed command fixed by its vendor, `bin/Tool_Binary` (filenaming.scripts)."
                ],
                exemption_blobs,
                {"tools/Install.ps1", "tools/Setup_Host.sh"},
                set(),
                [],
            ),
            (
                "a note naming a path that no longer breaks, or no longer exists, is stale",
                [
                    "Tool-fixed `tools/Install.ps1`, `tools/install-host.sh`, `tools/Gone.ps1` (filenaming.scripts)."
                ],
                {"tools/Install.ps1": plain, "tools/install-host.sh": exe},
                set(),
                set(),
                ["exempts 2 path(s)", "tools/install-host.sh, tools/Gone.ps1"],
            ),
            (
                "a note naming no path exempts nothing and is reported",
                ["Install scripts are named by the package format (filenaming.scripts)."],
                {"tools/Install.ps1": plain},
                {"tools/Install.ps1"},
                set(),
                ["names no path"],
            ),
            (
                "a path without the parenthesized id exempts nothing",
                ["A package format fixes `tools/Install.ps1` (filenaming.scripts is pending)."],
                {"tools/Install.ps1": plain},
                {"tools/Install.ps1"},
                set(),
                [],
            ),
        ]
        for label, notes_in, blobs_in, want_broken, want_undecided, want_notes in exemption_cases:
            with self.subTest(label):
                got = audit.file_naming_findings(
                    {"driftNotes": notes_in}, blobs_in, lambda path: "\x7fELF"
                )
                note_texts = [t for _, t in got if t.startswith("registry: ")]
                broken_text = next((t for _, t in got if " break " in t), "")
                undecided_text = next((t for _, t in got if "cannot decide" in t), "")
                good = (
                    all(kind == "DRIFT" for kind, _ in got)
                    and len(got) == len(note_texts) + bool(want_broken) + bool(want_undecided)
                    and len(note_texts) == (1 if want_notes else 0)
                    and all(w in note_texts[0] for w in want_notes)
                    and all(p in broken_text for p in want_broken)
                    and all(p not in broken_text for p in blobs_in.keys() - want_broken)
                    and all(p in undecided_text for p in want_undecided)
                    and all(p not in undecided_text for p in blobs_in.keys() - want_undecided)
                )
                self.assertTrue(good, f"{got}")

    def test_exemption_note_is_not_a_check_to_judge(self) -> None:
        exempt_note = (
            "A package format fixes `tools/Install.ps1` by that name (filenaming.scripts)."
        )
        self.assertEqual(
            audit.driftnote_findings(
                {"types": ["docs"], "driftNotes": [exempt_note]}, NOTE_SPEC, 0
            ),
            [],
            "an exemption note is not surfaced as a check to judge",
        )

    def test_unreadable_tree_reports_nothing(self) -> None:
        self.assertEqual(
            audit.file_naming_findings({}, None, lambda path: None),
            [],
            "an unreadable tree reports nothing here",
        )


class SecretStemCase(unittest.TestCase):
    """Holds the env and secret file name findings."""

    def test_secret_stem_cases(self) -> None:
        secret_stem_cases = [
            (
                "conforming names, examples, and the directory's own files pass",
                {
                    ".secrets/README.md",
                    ".secrets/.gitignore",
                    ".secrets/db_root_password.example",
                    ".secrets/mail_relay.env.example",
                    ".secrets/app_web-01_prod.env.example",
                    ".secrets/app_xn--bcher-kva_prod.env",
                    "deploy/.secrets/service_account.json.example",
                    "deploy/app_web-01_prod.env.example",
                    "docs/Mixed-Case.md",
                },
                set(),
            ),
            (
                "a capitalized, dotted, or bare name in a .secrets directory breaks",
                {
                    ".secrets/Mail_Relay.env.example",
                    ".secrets/.env.example",
                    ".secrets/app.web-01.prod.env",
                },
                {
                    ".secrets/Mail_Relay.env.example",
                    ".secrets/.env.example",
                    ".secrets/app.web-01.prod.env",
                },
            ),
            (
                "a .txt on a single credential in a .secrets directory breaks",
                {".secrets/app_token.txt.example", "deploy/.secrets/api_key.TXT"},
                {".secrets/app_token.txt.example", "deploy/.secrets/api_key.TXT"},
            ),
            (
                "a badly separated name, or a capitalized .env file elsewhere, breaks",
                {
                    ".secrets/app_-web.env",
                    "deploy/.secrets/api_token_",
                    "deploy/App_Prod.env",
                    "deploy/app_prod.ENV.example",
                    "deploy/app_prod.env.EXAMPLE",
                },
                {
                    ".secrets/app_-web.env",
                    "deploy/.secrets/api_token_",
                    "deploy/App_Prod.env",
                    "deploy/app_prod.ENV.example",
                    "deploy/app_prod.env.EXAMPLE",
                },
            ),
            (
                "a file nested below a .secrets directory, a tool-fixed env name, or another file is out of scope",
                {
                    "config/.secrets-notes/Odd-Name",
                    ".secrets/docs/Mixed-Case.md",
                    ".env",
                    "web/.env.example",
                    "web/.env.local",
                    "deploy/Notes.environment",
                },
                set(),
            ),
        ]
        for label, tree_in, want in secret_stem_cases:
            with self.subTest(label):
                got = audit.secret_stem_findings(tree_in)
                good = (not want and got == []) or (
                    len(got) == 1
                    and got[0][0] == "DRIFT"
                    and got[0][1].startswith(f"runtime-secrets: {len(want)} env or secret file(s)")
                    and all(p in got[0][1] for p in want)
                )
                self.assertTrue(good, f"{got}")

    def test_unreadable_tree_reports_nothing(self) -> None:
        self.assertEqual(
            audit.secret_stem_findings(None), [], "an unreadable tree reports nothing here"
        )

    def test_a_tree_alone_reads_no_signal_file(self) -> None:
        self.assertEqual(audit.secret_stem_findings({".gitignore", "compose.yml"}), [])

    def test_a_bare_secrets_directory_is_read_from_the_files_naming_it(self) -> None:
        """The directory is ignored by design, so a .gitignore line and a config path are the signals."""
        bare = (
            "runtime-secrets: {} tracked file(s) name or sit in a secrets directory not named "
            '.secrets/, which GOVERNANCE.md "Repo-Scoped Secrets" rules out. Rename it .secrets/ '
            "and update each: {}"
        )
        cases: list[tuple[str, dict[str, str], list[str], list[str]]] = [
            (
                "the issue's constructed case reports the directory and the .txt it names",
                {
                    ".gitignore": "secrets/\n",
                    ".vscode/launch.json": '{"args": ["--token-file=${workspaceFolder}/secrets/app_token.txt"]}\n',
                },
                ["secrets/app_token.txt (named in .vscode/launch.json)"],
                [".gitignore", ".vscode/launch.json"],
            ),
            (
                "each bare ignore pattern names the directory",
                {
                    ".gitignore": "/secrets\n",
                    "deploy/.gitignore": "**/secrets/\n",
                    "web/.gitignore": "secrets/*\n!secrets/*.example\n",
                    "ops/.gitignore": "secrets/**/*\n!secrets/**/*.example\n",
                },
                [],
                [".gitignore", "deploy/.gitignore", "ops/.gitignore", "web/.gitignore"],
            ),
            (
                "an unmarked secrets/ token is no path, and an unmarked compose file: value is one",
                {
                    "compose.yml": "# Docker secrets/configs are mounted read-only\nVAULT_KV_PATH: secrets/data/app\n",
                    ".vscode/launch.json": '{"env": {"VAULT_PATH": "secrets/myapp"}}\n',
                    "deploy/compose.yml": "secrets:\n  db:\n    file: secrets/Db_Password\n",
                },
                ["deploy/secrets/Db_Password (named in deploy/compose.yml)"],
                ["deploy/compose.yml"],
            ),
            (
                "a long-syntax bind source and an env_file path entry are host paths",
                {
                    "compose.yml": (
                        "services:\n  app:\n    env_file:\n      - path: ./secrets/App.env\n"
                        "    volumes:\n      - source: ./secrets\n        type: bind\n        target: /s\n"
                    ),
                },
                ["secrets/App.env (named in compose.yml)"],
                ["compose.yml"],
            ),
            (
                "a .gitignore line beside a code directory named secrets is no signal, anchored or floating",
                {
                    "deploy/.gitignore": "/secrets/\n",
                    "deploy/secrets/run.py": "",
                    "ops/.gitignore": "secrets/\n",
                    "ops/lib/secrets/run.py": "",
                },
                [],
                [],
            ),
            (
                "a relative editor path is no signal, even into a code directory",
                {
                    ".vscode/tasks.json": (
                        '{"options": {"cwd": "${workspaceFolder}/src"}, "command": "python ./secrets/manager.py"}\n'
                    ),
                    "src/secrets/manager.py": "",
                },
                [],
                [],
            ),
            (
                "the deprecated and file workspace variables root a path, and a composer.yml is no compose file",
                {
                    ".vscode/launch.json": '{"envFile": "${workspaceRoot}/secrets/app.env"}\n',
                    ".vscode/tasks.json": '{"command": "cat ${fileWorkspaceFolder}/.secrets/Api_Token.txt"}\n',
                    "composer.yml": "secrets:\n  s:\n    file: ./secrets/Db_Password\n",
                },
                [".secrets/Api_Token.txt (named in .vscode/tasks.json)"],
                [".vscode/launch.json"],
            ),
            (
                "an editor comment line is no path, and an anchored ignore line takes the same refusals",
                {
                    ".devcontainer/devcontainer.json": (
                        '{\n  // "mounts": ["source=${localWorkspaceFolder}/secrets/Api_Key,target=/k,type=bind"]\n}\n'
                    ),
                    ".vscode/launch.json": (
                        '{\n  // "envFile": "${workspaceFolder}/secrets/app.env"\n'
                        '  "envFile": "${workspaceFolder}/.secrets/app.env" // was ${workspaceFolder}/secrets/app.env\n'
                        '  "url": "http://localhost/${workspaceFolder}/secrets/x"\n}\n'
                    ),
                    ".gitignore": "legacy/secrets/\nlegacy/*/secrets/\n/tools/secrets\n",
                    "tools/secrets": "",
                },
                [],
                [],
            ),
            (
                "a workspace file is read, and a bind or mount source names no file",
                {
                    "App.code-workspace": '{"settings": {"python.envFile": "${workspaceFolder}/secrets/App.env"}}\n',
                    "deploy/compose.yml": "services:\n  a:\n    volumes:\n      - ./.secrets/Prod_Certs:/certs:ro\n",
                    ".devcontainer/devcontainer.json": (
                        '{"mounts": ["source=${localWorkspaceFolder}/.secrets/Api_Certs,target=/c,type=bind",'
                        ' {"source": "${localWorkspaceFolder}/.secrets/Prod_Certs", "type": "bind"},'
                        ' "type=bind,src=${localWorkspaceFolder}/.secrets/Web_Certs,dst=/c"],'
                        ' "runArgs": ["-v", "${localWorkspaceFolder}/.secrets/Run_Certs:/certs:ro"],'
                        ' "postCreateCommand": "mkdir -p ${containerWorkspaceFolder}/.secrets/Tls_Certs"}\n'
                    ),
                },
                ["secrets/App.env (named in App.code-workspace)"],
                ["App.code-workspace"],
            ),
            (
                "a workspace file resolves against its listed folder, and an editor bind with no extension names no file",
                {
                    "App.code-workspace": (
                        '{"folders": [{"path": "src"}], "settings": {"x": "${workspaceFolder}/secrets/manager.py"}}\n'
                    ),
                    "src/secrets/manager.py": "",
                    "src/secrets/__init__.py": "",
                    ".vscode/tasks.json": (
                        '{"command": "docker run -v ${workspaceFolder}/.secrets/Prod_Certs:/certs app",'
                        ' "detail": "Mounts the certs in ${workspaceFolder}/.secrets/Tls_Certs."}\n'
                    ),
                    ".secrets/README.md": "",
                },
                [],
                [],
            ),
            (
                "a compose secret or env file named secrets is a file, not a directory",
                {
                    "compose.yml": "secrets:\n  s:\n    file: ./secrets\nservices:\n  app:\n    env_file: ./secrets\n",
                },
                [],
                [],
            ),
            (
                "a floating ignore line is silenced only by a code directory below its own prefix",
                {
                    ".gitignore": "deploy/**/secrets/\n",
                    "deploy/app/compose.yml": "",
                    "src/app/secrets/manager.py": "",
                },
                [],
                [".gitignore"],
            ),
            (
                "a .gitignore naming .secrets is dropped, but not for a comment, and a plain path past the folder is none",
                {
                    ".gitignore": "/.secrets/*\n/secrets/\n",
                    "ops/.gitignore": "# TODO: move secrets/ to deploy/.secrets/\nsecrets/\n",
                    "web/.gitignore": "/secrets/generated.json\n",
                },
                [],
                ["ops/.gitignore"],
            ),
            (
                "a tracked directory, a devcontainer env file, a named volume, an exempt file, and prose are no secret file",
                {
                    ".vscode/launch.json": (
                        '["${workspaceFolder}/.secrets/Prod.Certs", "${workspaceFolder}/.secrets/README.md"]\n'
                        "// Put the token in ${workspaceFolder}/.secrets/app.env.\n"
                        '{"detail": "Put the token in ${workspaceFolder}/.secrets/app.env."}\n'
                    ),
                    ".secrets/Prod.Certs/tls_key.example": "",
                    ".devcontainer/devcontainer.json": (
                        '{"runArgs": ["--env-file", "${localWorkspaceFolder}/.secrets/App.env"]}\n'
                    ),
                    "compose.yml": "services:\n  a:\n    volumes:\n      - secrets:/run/secrets\n",
                },
                [],
                [],
            ),
            (
                "a negated ignore line, a secrets.json, and an interpolated secret file are no signal",
                {
                    "web/.gitignore": "!**/secrets/*.example\n",
                    ".vscode/settings.json": '{"x": "${workspaceFolder}/secrets.json"}\n',
                    "compose.yml": "secrets:\n  s:\n    file: ./.secrets/${ENV}_db_password\n",
                },
                [],
                [],
            ),
            (
                "a compose build context, as a mapping or a string, and a config file are host paths",
                {
                    "compose.yml": "services:\n  a:\n    build:\n      context: ./secrets\n",
                    "web/compose.yml": "services:\n  b:\n    build: ./secrets\n",
                    "deploy/compose.yml": "configs:\n  c:\n    file: ./secrets/App.txt\n",
                },
                ["deploy/secrets/App.txt (named in deploy/compose.yml)"],
                ["compose.yml", "deploy/compose.yml", "web/compose.yml"],
            ),
            (
                "a negated .secrets line also marks a .gitignore as moved",
                {".gitignore": "secrets/\n!.secrets/README.md\n"},
                [],
                [],
            ),
            (
                "a bind source alone, an !override list, and a tracked named file each count once",
                {
                    "bind/compose.yml": (
                        "services:\n  a:\n    volumes:\n      - type: bind\n        source: ./secrets\n        target: /s\n"
                    ),
                    "over/compose.yml": "services:\n  a:\n    volumes: !override\n      - ./secrets:/s\n",
                    "compose.yml": "secrets:\n  w:\n    file: ./.secrets/Web_Token.txt\n",
                    ".secrets/Web_Token.txt": "",
                },
                [".secrets/Web_Token.txt"],
                ["bind/compose.yml", "over/compose.yml"],
            ),
            (
                "a path separator variable, a root .devcontainer.json, a ? glob, and a null service",
                {
                    ".vscode/launch.json": '["${workspaceFolder}${pathSeparator}secrets${pathSeparator}app.txt"]\n',
                    ".devcontainer.json": '{"mounts": ["source=${localWorkspaceFolder}/ops/secrets,target=/s"]}\n',
                    "ops/README.md": "",
                    "deploy/.gitignore": "ap?/secrets/\n",
                    "deploy/ap1/README.md": "",
                    "compose.yml": "services:\n  app:\n",
                    ".devcontainer/c/devcontainer.json": '{"x": "${containerWorkspaceFolder}/secrets"}\n',
                },
                ["secrets/app.txt (named in .vscode/launch.json)"],
                [
                    ".devcontainer.json",
                    ".devcontainer/c/devcontainer.json",
                    ".vscode/launch.json",
                    "deploy/.gitignore",
                ],
            ),
            (
                "a floating line falls back to its own directory, and a first-line comment and a padded line are read",
                {
                    "ops/.gitignore": "secrets/\n",
                    "src/secrets/run.py": "",
                    "web/.gitignore": "secrets/ \n",
                    ".devcontainer/devcontainer.json": '// "${localWorkspaceFolder}/secrets/x"\n{}\n',
                    "compose.yml": "services:\n  a:\n    environment: !reset {}\n    volumes:\n      - ./secrets:/s\n",
                },
                [],
                ["compose.yml", "ops/.gitignore", "web/.gitignore"],
            ),
            (
                "a compose override file carrying !reset is read",
                {
                    "compose.override.yml": (
                        "services:\n  app:\n    ports: !reset []\n    volumes:\n      - ./secrets:/run/secrets\n"
                    ),
                },
                [],
                ["compose.override.yml"],
            ),
            (
                "a tracked file named secrets is no directory, and a tracked directory is no secret file",
                {
                    ".vscode/tasks.json": '{"command": "${workspaceFolder}/scripts/secrets"}\n',
                    "scripts/secrets": "",
                    "compose.yml": "services:\n  a:\n    volumes:\n      - ./tools/secrets:/usr/local/bin/secrets:ro\n",
                    "tools/secrets": "",
                    ".devcontainer/devcontainer.json": (
                        '{"mounts": ["source=${localWorkspaceFolder}/.secrets/Prod_Certs,target=/c,type=bind"]}\n'
                    ),
                    ".secrets/Prod_Certs/tls_key.example": "",
                },
                [],
                [],
            ),
            (
                "a .gitignore line left behind after the move to .secrets is no signal",
                {
                    ".gitignore": "/.secrets/*\n!/.secrets/*.example\n/secrets/\n",
                    "deploy/.gitignore": "/secrets/\n",
                    "deploy/.secrets/db_password.example": "",
                },
                [],
                [],
            ),
            (
                "a quoted compose file: value is a path, and a container-side value is not",
                {
                    "compose.yml": 'secrets:\n  db:\n    file: "secrets/Db_Password"\n',
                    "app/compose.yml": (
                        "services:\n  app:\n    working_dir: /app\n    environment:\n"
                        "      TOKEN_FILE: ./secrets/api_token.txt\n    command: cat ./secrets/x\n"
                    ),
                    "web/compose.yml": (
                        "services:\n  web:\n    working_dir: /app\n    command:\n      - ./secrets/api_token.txt\n"
                        "    healthcheck:\n      test:\n        - ./secrets/check\n"
                        "    volumes:\n      - type: volume\n        source: secrets\n        target: /s\n"
                    ),
                },
                ["secrets/Db_Password (named in compose.yml)"],
                ["compose.yml"],
            ),
            (
                "a committed credential beside its template does not make the directory code",
                {
                    "secrets/README.md": "",
                    "secrets/db_password.example": "",
                    "secrets/db_password": "",
                },
                [],
                ["secrets/db_password", "secrets/db_password.example"],
            ),
            (
                "a compose file naming one reports it beside its directory",
                {
                    "deploy/compose.yml": "secrets:\n  db_password:\n    file: ./secrets/db_password\n"
                },
                [],
                ["deploy/compose.yml"],
            ),
            (
                "a .secrets path, a mount point, a comment, a negation, and a GitHub secret are not a bare directory",
                {
                    ".gitignore": "# secrets/\n**/.secrets/*\n!secrets/x\nsecrets.json\n",
                    "docker-compose.yml": "secrets:\n  - source: db_password\n    target: /run/secrets/db_password\n",
                    ".vscode/tasks.json": '{"env": {"TOKEN": "${{ secrets.TOKEN }}", "F": "${workspaceFolder}/.secrets/db_password"}}\n',
                },
                [],
                [],
            ),
            (
                "an untracked file a .secrets path names is judged where it has an extension, a templated one once",
                {
                    ".vscode/launch.json": (
                        '["${workspaceFolder}/.secrets/Api_Token", "${workspaceFolder}${/}.secrets${/}web_token.txt",'
                        ' "${workspaceFolder}/.secrets/Mail_Relay.txt", "./.secrets/Cwd_Token.txt"]\n'
                    ),
                    ".secrets/Mail_Relay.txt.example": "",
                },
                [
                    ".secrets/web_token.txt (named in .vscode/launch.json)",
                    ".secrets/Mail_Relay.txt.example",
                ],
                [],
            ),
            (
                "a URL path or a cloud resource name with a secrets segment is no repo path",
                {
                    ".vscode/launch.json": '{"env": {"API": "http://localhost:8080/api/secrets/list"}}\n',
                    "compose.yml": "DB: projects/my-proj/secrets/db-password/versions/latest\n",
                },
                [],
                [],
            ),
            (
                "a whole-directory mount names the directory, and a secrets: key does not",
                {
                    "compose.yml": "secrets:\n  app_token:\n    file: x\nservices:\n  app:\n    volumes:\n      - ./secrets:/run/secrets:ro\n",
                    "deploy/compose.yml": "services:\n  app:\n    volumes:\n      - ./secrets/:/run/secrets\n",
                    ".devcontainer/devcontainer.json": (
                        '{"mounts": ["source=${localWorkspaceFolder}/secrets,target=/run/secrets,type=bind"]}\n'
                    ),
                },
                [],
                [".devcontainer/devcontainer.json", "compose.yml", "deploy/compose.yml"],
            ),
            (
                "a .secrets directory holding a nested template still has a named file judged",
                {
                    "compose.yml": "secrets:\n  s:\n    file: ./.secrets/Db_Password\n",
                    ".secrets/prod/db_password.example": "",
                },
                [".secrets/Db_Password (named in compose.yml)"],
                [],
            ),
            (
                "a nested template in a bare directory holds it",
                {
                    "compose.yml": "secrets:\n  s:\n    file: ./secrets/prod/db_password.txt\n",
                    "secrets/prod/db_password.txt.example": "",
                },
                [],
                ["compose.yml", "secrets/prod/db_password.txt.example"],
            ),
            (
                "a code directory named secrets is no signal from its template or a .gitignore line",
                {
                    ".gitignore": (
                        "src/app/secrets/__pycache__/\n**/secrets/__pycache__/\nsrc/**/secrets/__pycache__/\n"
                        "secrets/\n"
                    ),
                    "src/app/secrets/manager.py": "",
                    "src/app/secrets/config.toml.example": "",
                },
                [],
                [],
            ),
            (
                "a path relative to a task's cwd names no file",
                {
                    ".vscode/tasks.json": (
                        '{"options": {"cwd": "${workspaceFolder}/deploy"},'
                        ' "command": "cat ./.secrets/api_token.txt $PWD/.secrets/api_token.txt"}\n'
                    ),
                    "deploy/.secrets/api_token.txt.example": "",
                },
                ["deploy/.secrets/api_token.txt.example"],
                [],
            ),
            (
                "a bare directory holding its own .gitignore and templates is reported, and each template judged",
                {
                    "secrets/.gitignore": "*\n!*.example\n!README.md\n",
                    "secrets/README.md": "",
                    "secrets/Db_Password.example": "",
                },
                ["secrets/Db_Password.example"],
                ["secrets/.gitignore", "secrets/Db_Password.example"],
            ),
            (
                "a compose file under .devcontainer resolves against its own directory",
                {
                    ".devcontainer/docker-compose.yml": "secrets:\n  s:\n    file: ../secrets/app_token.txt\n"
                },
                ["secrets/app_token.txt (named in .devcontainer/docker-compose.yml)"],
                [".devcontainer/docker-compose.yml"],
            ),
            (
                "a path into a directory tracking code is not a bare directory",
                {
                    ".vscode/launch.json": (
                        '{"program": "${workspaceFolder}/src/app/secrets/manager.py",'
                        ' "cwd": "${workspaceFolder}/src/app/secrets/tools"}\n'
                    ),
                    ".vscode/settings.json": '{"files.exclude": {"src/app/secrets/*.pyc": true}}\n',
                    "compose.yml": "# see src/app/secrets/manager.py.\n",
                    "src/app/secrets/manager.py": "",
                    "src/app/secrets/tools/run.py": "",
                },
                [],
                [],
            ),
            (
                "one untracked name two files mention counts once",
                {
                    "compose.yml": "secrets:\n  s:\n    file: ./secrets/App_Token.txt\n",
                    ".vscode/launch.json": '["${workspaceFolder}/secrets/App_Token.txt"]\n',
                },
                ["secrets/App_Token.txt (named in .vscode/launch.json, compose.yml)"],
                [".vscode/launch.json", "compose.yml"],
            ),
            (
                "a secret file is judged, and a comment, interpolation, and a glob are no name",
                {
                    "compose.yml": (
                        "secrets:\n  t:\n    file: ./.secrets/App_Token.txt\nservices:\n  app:\n    volumes:\n"
                        "      # put the token in .secrets/web_token.\n      - ./.secrets/${ENV}_db_password:/p\n"
                    ),
                    ".vscode/settings.json": '{"files.exclude": {".secrets/*": true}}\n',
                },
                [".secrets/App_Token.txt (named in compose.yml)"],
                [],
            ),
            (
                "the devcontainer variables root a folder, a Windows path is read, and a shell cwd is not",
                {
                    ".devcontainer/app/devcontainer.json": (
                        '{"mounts": ["source=${localWorkspaceFolder}/secrets/app_token,'
                        'target=/run/secrets/app_token,type=bind"]}\n'
                    ),
                    ".devcontainer.json": '{"x": "${containerWorkspaceFolder}/.secrets/Api_Key"}\n',
                    "deploy/compose.yaml": "env: $(pwd)/.secrets/Mail_Token\n",
                    ".vscode/launch.json": '{"args": ["${workspaceFolder}\\\\.secrets\\\\Web_Token.txt"]}\n',
                },
                [
                    ".secrets/Web_Token.txt (named in .vscode/launch.json)",
                ],
                [".devcontainer/app/devcontainer.json"],
            ),
            (
                "a capitalized folder is read from a .gitignore line and a compose path",
                {
                    ".gitignore": "Secrets/\n",
                    "compose.yml": "secrets:\n  s:\n    file: ./Secrets/Db_Password\n",
                },
                ["Secrets/Db_Password (named in compose.yml)"],
                [".gitignore", "compose.yml"],
            ),
            (
                "a floating line is not silenced by a code directory its case cannot match",
                {
                    ".gitignore": "secrets/\n",
                    "src/App/Secrets/Store.cs": "",
                },
                [],
                [".gitignore"],
            ),
            (
                "a class matching a bare folder is a signal, and one able to name .secrets is not",
                {
                    ".gitignore": "[Ss]ecrets/\n",
                    "deploy/.gitignore": ".[Ss]ecrets/\n",
                    "ops/.gitignore": "/.Secrets/\n",
                },
                [],
                [".gitignore", "ops/.gitignore"],
            ),
            (
                "a .gitignore whose class names .secrets keeps its old guard line silent",
                {".gitignore": ".[Ss]ecrets/\n[Ss]ecrets/\n"},
                [],
                [],
            ),
        ]
        for label, texts, named, naming in cases:
            with self.subTest(label):
                tree = set(texts)
                got = audit.secret_stem_findings(tree, texts.get)
                stems = [t for _, t in got if " env or secret file(s) " in t]
                self.assertEqual(len(stems), 1 if named else 0, got)
                for path in named:
                    self.assertIn(path, stems[0])
                if named:
                    self.assertTrue(stems[0].startswith(f"runtime-secrets: {len(named)} env"), got)
                want = [bare.format(len(naming), ", ".join(naming))] if naming else []
                self.assertEqual([t for _, t in got if "not named .secrets/" in t], want)

    def test_a_dotted_folder_in_another_case_is_reported_and_its_files_judged(self) -> None:
        got = audit.secret_stem_findings({".Secrets/README.md", ".Secrets/Db_Password"})

        self.assertEqual(
            [t.rsplit(": ", 1)[1] for _, t in got],
            [".Secrets/Db_Password", ".Secrets/Db_Password, .Secrets/README.md"],
            got,
        )

    def test_a_devcontainer_compose_set_resolves_against_its_first_file(self) -> None:
        """Compose resolves every file of a set against the first one's directory, the project directory."""
        texts = {
            ".devcontainer/devcontainer.json": (
                '{\n  // the set\n  "dockerComposeFile": ["..\\\\docker-compose.yml", "docker-compose.extend.yml"]\n}\n'
            ),
            ".devcontainer/docker-compose.extend.yml": (
                "services:\n  app:\n    env_file: ./.secrets/App.env\n"
                "    volumes:\n      - ../secrets:/run/secrets\n"
            ),
            ".devcontainer/other/compose.yml": "services:\n  app:\n    env_file: ./.secrets/Web.env\n",
            "docker-compose.yml": "services: {}\n",
        }

        got = audit.secret_stem_findings(set(texts), texts.get)

        self.assertEqual(len(got), 1, got)
        self.assertTrue(
            got[0][1].endswith(
                ": .devcontainer/other/.secrets/Web.env (named in .devcontainer/other/compose.yml), "
                ".secrets/App.env (named in .devcontainer/docker-compose.extend.yml)"
            ),
            got,
        )

    def test_a_compose_file_two_sets_list_resolves_against_each_first_file(self) -> None:
        """Each devcontainer's set has its own project directory, so a shared file is read against both."""
        texts = {
            ".devcontainer/a/devcontainer.json": (
                '{"dockerComposeFile": ["../../docker-compose.yml", "../compose.shared.yml"]}\n'
            ),
            ".devcontainer/b/devcontainer.json": (
                '{"dockerComposeFile": ["compose.yml", "../compose.shared.yml"]}\n'
            ),
            ".devcontainer/compose.shared.yml": "services:\n  app:\n    env_file: ./.secrets/App.env\n",
        }

        got = audit.secret_stem_findings(set(texts), texts.get)

        self.assertEqual(len(got), 1, got)
        self.assertTrue(
            got[0][1].endswith(
                ": .devcontainer/b/.secrets/App.env (named in .devcontainer/compose.shared.yml), "
                ".secrets/App.env (named in .devcontainer/compose.shared.yml)"
            ),
            got,
        )

    def test_a_named_path_resolves_against_its_file_and_stays_inside_the_repo(self) -> None:
        """A compose path is relative to its own directory, an editor's to its workspace, and one leaving the repo is no signal."""
        texts = {
            "deploy/compose.yml": "secrets:\n  s:\n    file: ./.secrets/web_token.txt\n",
            "compose.yml": "secrets:\n  s:\n    file: ../secrets/Db_Password\n",
            "app/.vscode/launch.json": '["${workspaceFolder}/.secrets/Api_Token"]\n',
        }
        tree = set(texts) | {
            "deploy/.secrets/web_token.txt.example",
            "app/.secrets/Api_Token.example",
        }

        got = audit.secret_stem_findings(tree, texts.get)

        self.assertEqual(len(got), 1, got)
        self.assertTrue(
            got[0][1].endswith(
                ": app/.secrets/Api_Token.example, deploy/.secrets/web_token.txt.example"
            ),
            got,
        )

    def test_a_signal_file_read_with_no_text_is_undecided(self) -> None:
        got = audit.secret_stem_findings({".gitignore", "README.md"}, lambda path: None)

        self.assertEqual(
            got,
            [
                (
                    "DRIFT",
                    (
                        "runtime-secrets: could not read 1 file(s) to look for a bare "
                        "secrets/ directory or a named secret file, verify by hand: .gitignore"
                    ),
                )
            ],
        )

    def test_a_workflow_named_like_compose_is_not_read(self) -> None:
        read: list[str] = []

        def read_text(path: str) -> str:
            read.append(path)
            return "services:\n  a:\n    volumes:\n      - ./secrets:/s\n"

        got = audit.secret_stem_findings({".github/workflows/docker-compose.yml"}, read_text)

        self.assertEqual((got, read), ([], []))

    def test_a_gitignore_is_not_read_once_the_tree_carries_a_secrets_directory(self) -> None:
        read: list[str] = []

        def read_text(path: str) -> str:
            read.append(path)
            return "/secrets/\n"

        got = audit.secret_stem_findings({".gitignore", ".secrets/db_password.example"}, read_text)

        self.assertEqual((got, read), ([], []))

    def test_a_compose_file_that_does_not_parse_is_no_signal(self) -> None:
        """A template such as a Jinja compose file never parses, so reporting it would never clear."""
        texts = {
            "compose.yml": "services: [\n",
            "deploy/docker-compose.yml": "image: {{ image }}\n",
        }

        self.assertEqual(audit.secret_stem_findings(set(texts), texts.get), [])


class DeclaredTypesCase(unittest.TestCase):
    """Holds the reading of a registry entry's types value."""

    def test_declared_types(self) -> None:
        declared_types_cases = [
            ({"types": ["python", "docs"]}, ["python", "docs"], "a list of strings is read whole"),
            ({"types": ["python", 3, None]}, ["python"], "a mixed list keeps its string elements"),
            ({"types": "python"}, [], "a string is not a list"),
            ({"types": {"python": 1}}, [], "an object is not a list"),
            ({"types": None}, [], "a declared null reads as untyped"),
            ({}, [], "an absent field reads as untyped"),
        ]
        for entry_in, expected, label in declared_types_cases:
            with self.subTest(label):
                got = audit.declared_types(entry_in)
                self.assertEqual(got, expected)


class PythonDirectoriesCase(unittest.TestCase):
    """Holds the python directory coverage, undeclared root, and caller input findings."""

    def test_python_directory_coverage(self) -> None:
        python_directory_cases: list[tuple[Any, ...]] = [
            (
                {"pythonDirectories": ["Tools"]},
                {"Tools/a.py", "Extra/b.py"},
                1,
                "declared directory: one file outside it is missed",
            ),
            (
                {"pythonDirectories": ["Tools"]},
                {"Tools/a.py", "Tools/sub/b.py"},
                0,
                "declared directory: a nested file is still covered",
            ),
            (
                {},
                {"tools/a.py"},
                1,
                "no declaration and no root pyproject.toml: nothing is expected to cover it",
            ),
            (
                {},
                {"pyproject.toml", "a.py", "sub/b.py"},
                0,
                "no declaration, root pyproject.toml: '.' fallback covers everything",
            ),
            (
                {"pythonDirectories": ["Tools"]},
                None,
                0,
                "unreadable tree: coverage is unanswerable",
            ),
            (
                {"pythonDirectories": ["Tools"]},
                {f"Extra/f{i}.py" for i in range(7)},
                1,
                "more than 5 missed: one finding, capped listing",
            ),
        ]
        # Each case describes a Python-typed repo unless it names its own types, since an untyped one gets only the type advisory.
        python_directory_cases.append(
            ({"types": ["docs"]}, {"tools/x.py"}, 0, "an untyped repo gets only the type advisory")
        )
        python_directory_cases.append(
            (
                {"pythonDirectories": ["Tools"]},
                {"Tools/a.py", "hub_fetch_run.py"},
                0,
                "the carried hook helper sits outside every directory",
            )
        )
        python_directory_cases += [
            ({"types": "python"}, {"tools/x.py"}, 0, "a string types value reads as untyped"),
            ({"types": None}, {"tools/x.py"}, 0, "a null types value reads as untyped"),
        ]
        for entry_in, tree_in, expected, label in python_directory_cases:
            with self.subTest(label):
                got_findings = audit.python_directory_coverage_findings(
                    {"types": ["python"], **entry_in}, tree_in
                )
                self.assertEqual(len(got_findings), expected)
                if expected and len(tree_in or []) > 5:
                    self.assertIn(
                        "more",
                        got_findings[0][1],
                        "missing the '... and N more' cap",
                    )

    def test_python_undeclared_root(self) -> None:
        undeclared_root_cases: list[tuple[Any, ...]] = [
            (
                {},
                {"pyproject.toml", "tests/test_a.py", "a.py"},
                1,
                "a lint-only root suite nothing runs",
            ),
            (
                {},
                {"pyproject.toml", "tests/test_a.py", "uv.lock"},
                0,
                "a locked root runs by default",
            ),
            (
                {},
                {"pyproject.toml", "tests/test_a.py", "requirements.txt"},
                0,
                "a pip root runs by default",
            ),
            (
                {},
                {"pyproject.toml", "tests/test_a.py", "docs/requirements.txt"},
                1,
                "a nested manifest is not the root's",
            ),
            (
                {"pythonDirectories": ["."]},
                {"pyproject.toml", "tests/test_a.py"},
                0,
                "a declared root runs",
            ),
            ({}, {"pyproject.toml", "a.py"}, 0, "no root suite to run"),
            (
                {},
                {"pyproject.toml", "tests/App.Tests/App.Tests.csproj"},
                0,
                "a tests/ holding no Python",
            ),
            ({}, None, 0, "unreadable tree reports nothing"),
        ]
        undeclared_root_cases.append(
            (
                {"types": ["docs"]},
                {"pyproject.toml", "tests/test_a.py"},
                0,
                "an untyped repo gets only the type advisory",
            )
        )
        undeclared_root_cases += [
            (
                {"types": "python"},
                {"pyproject.toml", "tests/test_a.py"},
                0,
                "a string types value reads as untyped",
            ),
            (
                {"types": None},
                {"pyproject.toml", "tests/test_a.py"},
                0,
                "a null types value reads as untyped",
            ),
        ]
        for entry_in, tree_in, expected, label in undeclared_root_cases:
            with self.subTest(label):
                got = len(
                    audit.python_undeclared_root_findings(
                        {"types": ["python"], **entry_in}, tree_in
                    )
                )
                self.assertEqual(got, expected)

    def test_python_directories_caller(self) -> None:
        py_caller_uses = "    uses: ./.github/workflows/validate-task.yml\n"
        py_caller_block = (
            "jobs:\n  validate:\n"
            + py_caller_uses
            + "    with:\n      python-directories: |\n        Tools\n"
        )
        py_caller_plain = (
            "jobs:\n  validate:\n" + py_caller_uses + "    with:\n      python-directories: Tools\n"
        )
        py_caller_no_with = "jobs:\n  validate:\n" + py_caller_uses
        py_caller_no_hub = "jobs:\n  validate:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo inline lint\n"
        py_caller_path = ".github/workflows/test-pull-request.yml"
        python_caller_cases = [
            (
                py_caller_path,
                py_caller_block,
                {"pythonDirectories": ["Tools"]},
                0,
                "block scalar matches the registry",
            ),
            (
                py_caller_path,
                py_caller_block,
                {"pythonDirectories": ["Other"]},
                1,
                "block scalar disagrees with the registry",
            ),
            (
                py_caller_path,
                py_caller_plain,
                {"pythonDirectories": ["Tools"]},
                0,
                "plain single value matches the registry",
            ),
            (
                py_caller_path,
                py_caller_no_with,
                {},
                0,
                "no with: input, registry declares none - both empty",
            ),
            (
                py_caller_path,
                py_caller_no_with,
                {"pythonDirectories": ["Tools"]},
                1,
                "no with: input, registry declares one",
            ),
            (
                ".github/workflows/other.yml",
                py_caller_block,
                {"pythonDirectories": ["Other"]},
                0,
                "not a scoped caller path",
            ),
            (
                py_caller_path,
                py_caller_no_hub,
                {"pythonDirectories": ["Tools"]},
                0,
                "validate job never reaches validate-task.yml",
            ),
            (
                py_caller_path,
                py_caller_block.replace("  validate:\n", "  check:\n"),
                {"pythonDirectories": ["Tools"]},
                1,
                "a renamed job is reported, not skipped",
            ),
            (
                py_caller_path,
                "jobs:\n  validate:\n"
                + py_caller_uses
                + "    with: { python-directories: Tools }\n",
                {},
                1,
                "a flow-mapping with: is reported, not misread as declaring nothing",
            ),
            (
                py_caller_path,
                py_caller_block + "  validate-extra:\n" + py_caller_uses,
                {"pythonDirectories": ["Tools"]},
                1,
                "a second job calling validate-task.yml is reported beside a matching validate",
            ),
        ]
        py_caller_root = py_caller_plain.replace(
            "python-directories: Tools", "python-directories: ."
        )
        python_caller_cases.append(
            (
                py_caller_path,
                py_caller_root,
                {},
                1,
                "naming the root is not the same as naming nothing",
            )
        )
        py_caller_folded = py_caller_block.replace(
            "python-directories: |\n        Tools\n",
            "python-directories: >\n        Tools\n        Other\n",
        )
        py_caller_commented = py_caller_plain.replace(
            "    with:\n", "    with:\n        # a note indented past the keys\n"
        )
        python_caller_cases.append(
            (
                py_caller_path,
                py_caller_commented,
                {"pythonDirectories": ["Tools"]},
                0,
                "a deeper comment hides no key",
            )
        )
        py_caller_tabbed = py_caller_plain.replace(
            "python-directories: Tools", "python-directories: Too\tls"
        )
        py_caller_value = py_caller_plain.replace(
            "python-directories: Tools", "python-directories: {}"
        )
        python_caller_cases += [
            (
                py_caller_path,
                py_caller_value.format('&dirs "Tools\\nOther"'),
                {},
                1,
                "an anchored escaped double-quoted value is decoded",
            ),
            (
                py_caller_path,
                py_caller_value.format('!!str "Tools\\nOther"'),
                {},
                1,
                "a tagged escaped double-quoted value is decoded",
            ),
            (
                py_caller_path,
                py_caller_value.format('\n        "Tools\\nOther"'),
                {},
                1,
                "an escaped double-quoted value on the next line is decoded",
            ),
            (
                py_caller_path,
                py_caller_value.format(
                    '\n      # a note at the key column\n        "Tools\\nOther"'
                ),
                {},
                1,
                "a comment at the key column hides no next-line value",
            ),
            (
                py_caller_path,
                py_caller_value.format('"Tools\n        \\nOther"'),
                {"pythonDirectories": ["Tools"]},
                1,
                "an escape on a double-quoted value's continuation line is decoded",
            ),
            (
                py_caller_path,
                py_caller_value.format('"Tools"  # a\\b'),
                {"pythonDirectories": ["Tools"]},
                0,
                "a backslash past the closing quote is not an escape",
            ),
            (
                py_caller_path,
                py_caller_value.format("&dirs >\n        Tools"),
                {"pythonDirectories": ["Tools"]},
                0,
                "an anchored folded scalar reads as its value",
            ),
            (
                py_caller_path,
                py_caller_folded,
                {"pythonDirectories": ["Other", "Tools"]},
                1,
                "a folded scalar joins its lines into one entry",
            ),
            (
                py_caller_path,
                py_caller_tabbed,
                {"pythonDirectories": ["Too\tls"]},
                0,
                "a tab the YAML reader refuses leaves the file to the interface check",
            ),
        ]
        for path_in, text_in, entry_in, expected, label in python_caller_cases:
            with self.subTest(label):
                got = len(audit.python_directories_caller_findings(path_in, text_in, entry_in))
                self.assertEqual(got, expected)
        py_caller_escaped = py_caller_plain.replace(
            "python-directories: Tools", 'python-directories: "Tools\\nOther"'
        )
        escaped_got = audit.python_directories_caller_findings(
            py_caller_path, py_caller_escaped, {"pythonDirectories": ["Other", "Tools"]}
        )
        self.assertEqual(escaped_got, [], "an escaped double-quoted value is decoded")


class WorkflowModelCallerFindingsCase(unittest.TestCase):
    """Holds the pull request caller's workflow-model input against the registry's model."""

    path = ".github/workflows/test-pull-request.yml"
    caller = (
        "jobs:\n  validate:\n    uses: o/h/.github/workflows/validate-task.yml@x\n{0}"
        "  check-workflow-status:\n    runs-on: ubuntu-latest\n"
    )

    def findings(self, with_block, model, path=None):
        text = self.caller.format(with_block)
        return audit.workflow_model_caller_findings(path or self.path, text, model)

    def test_operational_caller_passing_operational_is_clean(self) -> None:
        got = self.findings("    with:\n      workflow-model: operational\n", "operational")
        self.assertEqual(got, [])

    def test_operational_caller_without_the_input_drifts(self) -> None:
        got = self.findings("", "operational")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "DRIFT")
        self.assertIn("passes none, registry declares operational", got[0][1])

    def test_release_caller_passing_another_value_drifts(self) -> None:
        got = self.findings("    with:\n      workflow-model: operational\n", "release")
        self.assertEqual(len(got), 1)
        self.assertIn("passes 'operational', registry declares release", got[0][1])

    def test_release_caller_omitting_or_naming_release_is_clean(self) -> None:
        self.assertEqual(self.findings("", "release"), [])
        self.assertEqual(self.findings("    with:\n      workflow-model: release\n", "release"), [])

    def test_an_empty_value_is_not_the_default(self) -> None:
        got = self.findings("    with:\n      workflow-model: ''\n", "release")
        self.assertEqual(len(got), 1)

    def test_a_structured_value_is_not_the_default(self) -> None:
        got = self.findings("    with:\n      workflow-model: {model: release}\n", "release")
        self.assertEqual(len(got), 1)
        self.assertIn("passes a mapping or a sequence, registry declares release", got[0][1])

    def test_other_callers_and_jobs_are_not_read(self) -> None:
        publisher = ".github/workflows/publish-release.yml"
        self.assertEqual(self.findings("", "operational", publisher), [])
        no_task = "jobs:\n  validate:\n    runs-on: ubuntu-latest\n"
        self.assertEqual(
            audit.workflow_model_caller_findings(self.path, no_task, "operational"), []
        )

    def test_the_no_build_snippet_is_an_operational_caller(self) -> None:
        snippet = audit.ROOT / "catalog" / "snippets" / "workflows" / "test-pull-request.yml"
        text = snippet.read_text(encoding="utf-8")
        self.assertEqual(audit.workflow_model_caller_findings(self.path, text, "operational"), [])

    def test_the_model_falls_back_to_defaults_then_release(self) -> None:
        self.assertEqual(
            audit.workflow_model_of({"workflowModel": "operational"}, {}), "operational"
        )
        self.assertEqual(
            audit.workflow_model_of({}, {"workflowModel": "operational"}), "operational"
        )
        self.assertEqual(audit.workflow_model_of({}, {}), "release")


class ConcurrencyCallerFindingsCase(unittest.TestCase):
    """Holds the publisher and merge-bot concurrency group checks."""

    def setUp(self) -> None:

        snippet_dir = audit.ROOT / "catalog" / "snippets" / "workflows"
        pub_path = ".github/workflows/publish-release.yml"
        bot_path = ".github/workflows/merge-bot-pull-request.yml"
        pub_text = (snippet_dir / "publish-release.yml").read_text(encoding="utf-8")
        bot_text = (snippet_dir / "merge-bot-pull-request.yml").read_text(encoding="utf-8")
        pub_group = "group: ${{ github.workflow }}-${{ github.ref }}"
        pub_line = next(ln for ln in pub_text.splitlines() if ln.strip().startswith("group:"))
        bot_line = next(ln for ln in bot_text.splitlines() if ln.strip().startswith("group:"))
        pub_value = pub_line.split("group:", 1)[1].strip()
        sched_line = (
            "  "
            + pub_group
            + "${{ github.event_name == 'schedule' && format('-{0}', github.run_id) || '' }}"
        )
        pub_gate = (
            "if: ${{ needs.plan.outputs.publish == 'true' && needs.validate.result == 'success' }}"
        )
        sched_gate = "if: ${{ github.event_name == 'workflow_dispatch' && needs.plan.outputs.publish == 'true' }}"
        sched_text = re.sub(
            r"(?s)\non:\n.*?\n(?=\S)",
            "\non:\n  workflow_dispatch:\n  schedule:\n    - cron: '0 2 * * MON'\n\n",
            pub_text.replace(pub_line, sched_line).replace(pub_gate, sched_gate),
            count=1,
        )
        self.fx = (
            pub_path,
            bot_path,
            pub_text,
            bot_text,
            pub_group,
            pub_line,
            bot_line,
            pub_value,
            sched_line,
            pub_gate,
            sched_gate,
            sched_text,
        )

    def test_refusal_messages(self) -> None:
        (
            pub_path,
            bot_path,
            pub_text,
            bot_text,
            pub_group,
            pub_line,
            bot_line,
            pub_value,
            sched_line,
            *_,
        ) = self.fx
        refusal_cases = [
            (
                pub_path,
                pub_text.replace(pub_line, sched_line),
                "cannot read its schedule",
                "a schedule-only suffix names what keeps it from qualifying",
            ),
            (
                bot_path,
                bot_text.replace(bot_line, bot_line + sched_line.split("github.ref }}", 1)[1]),
                "group is",
                "a merge-bot schedule suffix gets no publisher advice",
            ),
            (
                pub_path,
                "concurrency: ${{ github.workflow }}-${{ github.ref }}\n  -extra\njobs:\n  x: {}\n",
                "group is",
                "a continuation after an inline scalar folds into the group",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  group: >-\n    " + pub_value),
                "",
                "a folded group reads as its value",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, pub_line + "\n      more"),
                "group is",
                "a continuation line folds into the group",
            ),
            (pub_path, "concurrency: { group: x }\n", "group is", "a flow mapping is read"),
            (pub_path, "concurrency: [x]\n", "cannot read", "a sequence is refused"),
            (
                pub_path,
                pub_text.replace("concurrency:\n", "concurrency: &c\n", 1),
                "",
                "an anchor on the key",
            ),
            (
                pub_path,
                pub_text.replace(
                    pub_line,
                    "  "
                    + pub_group
                    + "${{ github.event_name == 'push' && format('-{0}', github.run_id) || '' }}",
                ),
                "group is",
                "a suffix without the main clause",
            ),
            (
                pub_path,
                pub_text.replace(
                    pub_line,
                    "  " + pub_group + "${{ format('-{0}{1}', github.run_id, github.event_name) }}",
                ),
                "group is",
                "an unconditional suffix naming event_name",
            ),
        ]
        for path_in, text_in, needle, label in refusal_cases:
            with self.subTest(label):
                got = audit.concurrency_caller_findings(path_in, text_in)
                if needle == "":
                    self.assertFalse(got, f"{label} -> {got}")
                else:
                    self.assertTrue(any(needle in f[1] for f in got), f"{label} -> {got}")

    def test_finding_counts(self) -> None:
        (
            pub_path,
            bot_path,
            pub_text,
            bot_text,
            pub_group,
            pub_line,
            bot_line,
            pub_value,
            sched_line,
            pub_gate,
            sched_gate,
            sched_text,
        ) = self.fx
        concurrency_cases = [
            (
                pub_path,
                sched_text,
                0,
                "a schedule-only suffix on a publisher whose schedule never publishes",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, sched_line),
                1,
                "a schedule-only suffix on a push-triggered publisher",
            ),
            (
                pub_path,
                sched_text.replace(sched_gate, pub_gate),
                1,
                "a schedule-only suffix on a publisher whose schedule publishes",
            ),
            (
                pub_path,
                sched_text.replace(
                    sched_gate,
                    "if: ${{ github.event_name == 'workflow_dispatch' || needs.plan.outputs.publish == 'true' }}",
                ),
                1,
                "a dispatch gate behind a disjunction",
            ),
            (
                pub_path,
                sched_text.replace(
                    sched_gate,
                    "if: >-\n      github.event_name == 'workflow_dispatch' &&\n      needs.plan.outputs.publish == 'true'",
                ),
                0,
                "a folded dispatch gate",
            ),
            (
                pub_path,
                sched_text.replace(
                    sched_gate,
                    "if: >- # note\n      github.event_name == 'workflow_dispatch' &&\n      needs.plan.outputs.publish == 'true'",
                ),
                0,
                "a folded dispatch gate with a comment on its key line",
            ),
            (
                pub_path,
                sched_text.replace("\non:\n", "\non: # triggers\n", 1),
                0,
                "a comment on the on: key line",
            ),
            (
                pub_path,
                sched_text.replace(
                    sched_gate, "if: ${{ github.event_name == 'workflow_dispatch' }}"
                ),
                0,
                "a dispatch test alone",
            ),
            (
                pub_path,
                sched_text.replace(
                    sched_gate,
                    "if: ${{ needs.plan.outputs.publish == 'true' && (github.event_name != 'schedule') }}",
                ),
                1,
                "a parenthesized not-schedule test as the last conjunct",
            ),
            (
                pub_path,
                sched_text.replace(
                    sched_gate,
                    "if: ${{ needs.plan.outputs.publish == 'true' && (github.event_name == 'workflow_dispatch') }}",
                ),
                0,
                "a parenthesized dispatch test as the last conjunct",
            ),
            (
                pub_path,
                sched_text.replace(
                    sched_gate,
                    "if: ${{ !(needs.plan.outputs.publish == 'true' && (github.event_name == 'workflow_dispatch')) }}",
                ),
                1,
                "a dispatch test inside a negated group",
            ),
            (
                pub_path,
                sched_text.replace("build-release-task.yml@", "other-task.yml@"),
                1,
                "a schedule-only suffix with no release job",
            ),
            (
                pub_path,
                sched_text.replace("  schedule:\n", "  push:\n  schedule:\n", 1),
                1,
                "a schedule-only suffix beside a push trigger",
            ),
            (pub_path, pub_text, 0, "the reference publisher, run_id suffix included"),
            (bot_path, bot_text, 0, "the reference merge-bot"),
            (
                pub_path,
                pub_text.replace(
                    "github.actor != 'ptr727-codegen[bot]' && github.actor != 'dependabot[bot]'",
                    "github.actor != 'dependabot[bot]' && github.actor != 'ptr727-codegen[bot]'",
                ),
                0,
                "the allowlisted actors in the other order",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  " + pub_group),
                0,
                "a bare ref-scoped publisher group",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  group: ${{ github.workflow }}"),
                1,
                "a global publisher group",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  group: publish"),
                1,
                "a literal publisher group",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  " + pub_group + "-extra"),
                1,
                "a literal publisher suffix",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  " + pub_group + "${{ github.sha }}"),
                1,
                "a non-run_id publisher suffix",
            ),
            (
                pub_path,
                pub_text.replace("cancel-in-progress: false", "cancel-in-progress: true"),
                1,
                "a cancelling publisher",
            ),
            (
                bot_path,
                bot_text.replace(bot_line, "  group: ${{ github.workflow }}-${{ github.ref }}"),
                1,
                "a ref-keyed merge-bot",
            ),
            (
                bot_path,
                bot_text.replace("cancel-in-progress: false", "cancel-in-progress: true"),
                1,
                "a cancelling merge-bot",
            ),
            (
                bot_path,
                bot_text.replace(bot_line, bot_line + "${{ github.run_id }}"),
                1,
                "a run_id suffix is the publisher's alone",
            ),
            (
                pub_path,
                re.sub(r"(?s)\nconcurrency:.*?cancel-in-progress: \w+\n", "\n", pub_text),
                1,
                "no concurrency block",
            ),
            (
                pub_path,
                pub_text.replace(
                    "github.actor != 'dependabot[bot]'", "github.actor == 'dependabot[bot]'"
                ),
                1,
                "an actor predicate that is not an exclusion list",
            ),
            (
                pub_path,
                pub_text.replace("refs/heads/main", "refs/heads/develop"),
                1,
                "a suffix keyed on another branch",
            ),
            (
                pub_path,
                pub_text.replace(
                    "github.actor != 'dependabot[bot]'", "github.actor != 'other[bot]'"
                ),
                1,
                "an actor list that is not the plan task's allowlist",
            ),
            (
                pub_path,
                pub_text.replace("ptr727-codegen[bot]", "ptr727-codegen [bot]"),
                1,
                "whitespace inside an allowlisted actor name",
            ),
            (
                bot_path,
                bot_text.replace(bot_line, bot_line + pub_value.split("github.ref }}", 1)[1]),
                1,
                "the push-only suffix is the publisher's alone",
            ),
            (
                bot_path,
                bot_text.replace(bot_line, bot_line + sched_line.split("github.ref }}", 1)[1]),
                1,
                "the schedule-only suffix is the publisher's alone",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  group: '" + pub_value.replace("'", "''") + "'"),
                0,
                "a single-quoted publisher group",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  group: " + pub_value + " # note"),
                0,
                "a trailing comment",
            ),
            (
                pub_path,
                pub_text.replace("concurrency:\n", "concurrency: # note\n", 1),
                0,
                "a comment on the key line",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  group: ${{github.workflow}}-${{github.ref}}"),
                0,
                "tight expression spacing",
            ),
            (
                pub_path,
                pub_text.replace("cancel-in-progress: false", "cancel-in-progress: ${{ false }}"),
                0,
                "an expression false",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  group: >-\n    " + pub_value),
                0,
                "a folded group reads as its value",
            ),
            (
                pub_path,
                pub_text.replace(pub_line, "  " + pub_group + "${{ github.run_id }}"),
                1,
                "an unconditional run_id suffix",
            ),
            (
                pub_path,
                pub_text.replace(
                    pub_line,
                    "  " + pub_group + "${{ github.ref == 'refs/heads/main' && github.run_id }}",
                ),
                1,
                "a suffix naming no event_name",
            ),
            (
                pub_path,
                pub_text.replace(
                    pub_line,
                    "  "
                    + pub_group
                    + "${{ github.sha }}-${{ github.event_name }}${{ github.run_id }}",
                ),
                1,
                "several suffix expressions",
            ),
            (
                bot_path,
                "concurrency: ${{ github.workflow }}-${{ github.event.pull_request.number }}\n",
                0,
                "a bare scalar merge-bot group",
            ),
            (
                pub_path,
                "concurrency: ${{ github.workflow }}-${{ github.ref }}\njobs:\n  x:\n    concurrency:\n      group: y\n",
                0,
                "a bare scalar group, job-level concurrency ignored",
            ),
            (
                ".github/workflows/test-pull-request.yml",
                "concurrency:\n  group: x\n",
                0,
                "another workflow is not read",
            ),
        ]
        for path_in, text_in, expected, label in concurrency_cases:
            with self.subTest(label):
                got = len(audit.concurrency_caller_findings(path_in, text_in))
                self.assertEqual(got, expected)


class RepoIdentityCase(unittest.TestCase):
    """Holds the repo_identity URL parsing check."""

    def test_repo_identity(self) -> None:
        id_cases = [
            ("https://github.com/owner/Repo", "owner/repo"),
            ("https://github.com/owner/Repo/", "owner/repo"),
            # Padding still resolves here, since a padded entry must read as an entry that exists.
            ("  https://github.com/owner/Repo  ", "owner/repo"),
            ("https://github.com/owner/Repo.git", "owner/repo"),
            ("https://github.com/owner/Repo.github", "owner/repo.github"),
            ("https://github.com/owner/Repo?tab=readme", None),
            ("https://github.com/owner/Repo#readme", None),
            ("https://github.com/owner/Repo.git?tab=readme", None),
            ("https://github.com/owner?tab=readme/Repo", None),
            ("https://github.com/owner#readme/Repo", None),
            ("http://github.com/owner/Repo", None),
            ("https://gitlab.com/owner/Repo", None),
            ("not a url", None),
            (None, None),
            (42, None),
        ]
        for url, want in id_cases:
            with self.subTest(str(url)):
                self.assertEqual(audit.repo_identity(url), want)


class OwnerReposCase(unittest.TestCase):
    """Holds the owner_repos fork, pagination, and gh failure checks."""

    def test_forks_dropped_and_pagination_stops_on_short_page(self) -> None:
        pages = {
            1: [{"name": f"r{i}", "fork": False} for i in range(100)],
            2: [{"name": "kept", "fork": False}, {"name": "dropped-fork", "fork": True}],
        }
        fake = lambda path, ok404=False: (
            {"login": "owner"} if path == "user" else pages[int(path.rsplit("page=", 1)[1])]
        )
        with mock.patch.object(audit, "gh", fake):
            got = {r["name"] for r in audit.owner_repos("owner")}
        want = {f"r{i}" for i in range(100)} | {"kept"}
        self.assertEqual(got, want, "owner_repos forks/pagination wrong")

    def test_none_user_response_raises_mismatch(self) -> None:
        def fake(path: str, ok404: bool = False) -> Any:
            return None if path == "user" else []

        with mock.patch.object(audit, "gh", fake), self.assertRaises(RuntimeError) as ctx:
            audit.owner_repos("owner")
        self.assertIn("not registry owner 'owner'", str(ctx.exception))

    def test_differently_cased_login_matches(self) -> None:
        fake = lambda path, ok404=False: {"login": "Owner"} if path == "user" else []
        with mock.patch.object(audit, "gh", fake):
            audit.owner_repos("owner")

    def test_none_page_fails_loud(self) -> None:
        fake = lambda path, ok404=False: {"login": "owner"} if path == "user" else None
        with mock.patch.object(audit, "gh", fake), self.assertRaises(RuntimeError):
            audit.owner_repos("owner")


class GhAbsenceCase(unittest.TestCase):
    """Holds gh's reading of a 404 by its printed status notation rather than by its prose."""

    def call(self, stderr: str, ok404: bool = True) -> Any:
        failed = subprocess.CompletedProcess(["gh"], 1, "", stderr)
        with mock.patch.object(audit.subprocess, "run", return_value=failed):
            return audit.gh("repos/example/widget/contents/README.md", ok404=ok404)

    def test_a_printed_404_reads_as_absence(self) -> None:
        self.assertIsNone(self.call("gh: Not Found (HTTP 404)"))
        self.assertIsNone(self.call("gh: something reworded (HTTP 404)"))

    def test_prose_without_the_notation_is_an_unexpected_failure(self) -> None:
        for stderr in ("gh: Not Found", "error: HTTP 404 from proxy", "dial tcp: lookup failed"):
            with self.subTest(stderr=stderr), self.assertRaises(RuntimeError):
                self.call(stderr)

    def test_another_printed_status_is_an_unexpected_failure(self) -> None:
        for stderr in ("gh: Forbidden (HTTP 403)", "gh: Validation Failed (HTTP 422)"):
            with self.subTest(stderr=stderr), self.assertRaises(RuntimeError):
                self.call(stderr)

    def test_a_404_without_ok404_still_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            self.call("gh: Not Found (HTTP 404)", ok404=False)


class MembershipFindingsCase(unittest.TestCase):
    """Holds the registry membership findings against a faked owner_repos."""

    def test_membership_findings(self) -> None:
        cases = [
            (
                "a repo missing from the registry is a DEFECT",
                [{"name": "New", "full_name": "owner/New", "archived": False}],
                {"owner": "owner", "repos": []},
                [("DEFECT", "owner/New: exists on GitHub with no registry/repos.json entry")],
            ),
            (
                "archived on GitHub but not in the registry is DRIFT",
                [{"name": "Old", "full_name": "owner/Old", "archived": True}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Old",
                            "url": "https://github.com/owner/Old",
                            "status": "cataloged",
                        }
                    ],
                },
                [
                    (
                        "DRIFT",
                        "Old: archived on GitHub but registry status is 'cataloged' (reconcile to 'archived')",
                    )
                ],
            ),
            (
                "registry says archived but GitHub disagrees is DRIFT",
                [{"name": "Back", "full_name": "owner/Back", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Back",
                            "url": "https://github.com/owner/Back",
                            "status": "archived",
                        }
                    ],
                },
                [
                    (
                        "DRIFT",
                        "Back: registry status is 'archived' but GitHub reports it unarchived "
                        + "(reconcile status, or verify it was not unarchived by mistake)",
                    )
                ],
            ),
            (
                "matched, not archived either side, is clean",
                [{"name": "Fine", "full_name": "owner/Fine", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Fine",
                            "url": "https://github.com/owner/Fine",
                            "status": "cataloged",
                        }
                    ],
                },
                [],
            ),
            (
                "identity matching is case-insensitive",
                [{"name": "MixedCase", "full_name": "Owner/MixedCase", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "mixedcase",
                            "url": "https://github.com/owner/mixedcase",
                            "status": "cataloged",
                        }
                    ],
                },
                [],
            ),
            (
                "an incidental URL whitespace difference does not miss a real match",
                [{"name": "Spacey", "full_name": "owner/Spacey", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Spacey",
                            "url": "  https://github.com/owner/Spacey  ",
                            "status": "cataloged",
                        }
                    ],
                },
                [],
            ),
            (
                "a same-named repo under a different owner is not treated as a match",
                [{"name": "Shared", "full_name": "owner/Shared", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Shared",
                            "url": "https://github.com/someone-else/Shared",
                            "status": "cataloged",
                        }
                    ],
                },
                [("DEFECT", "owner/Shared: exists on GitHub with no registry/repos.json entry")],
            ),
        ]
        for label, gh_repos, spec_registry, want in cases:
            with self.subTest(label):
                fake = lambda owner, r=gh_repos: r
                with mock.patch.object(audit, "owner_repos", fake):
                    got = audit.membership_findings({"registry": spec_registry})
                self.assertEqual(got, want)


class IntentCanonicalRelCase(unittest.TestCase):
    """Holds the intent_ref canonical resolution check."""

    def test_intent_ref_canonical(self) -> None:
        canonical_cases = [
            (
                "no reference or intentRef falls back to the file's own path",
                {},
                "AGENTS.md",
                "AGENTS.md",
            ),
            (
                "an intentRef equal to the path is itself the canonical",
                {"intentRef": "GOVERNANCE.md"},
                "GOVERNANCE.md",
                "GOVERNANCE.md",
            ),
            (
                "an anchored intentRef strips the anchor and keeps the whole file",
                {"intentRef": "GOVERNANCE.md#line-endings"},
                ".editorconfig",
                "GOVERNANCE.md",
            ),
            (
                "reference wins over intentRef when the manifest sets both",
                {"reference": "catalog/snippets/configs/.codecov.yml", "intentRef": "WORKFLOW.md"},
                ".codecov.yml",
                "catalog/snippets/configs/.codecov.yml",
            ),
            (
                "a literal '#' in reference is preserved rather than treated as an anchor",
                {"reference": "docs/notes#1.md"},
                "notes.md",
                "docs/notes#1.md",
            ),
            (
                "a literal '#' in path is preserved rather than treated as an anchor",
                {},
                "docs/notes#1.md",
                "docs/notes#1.md",
            ),
            (
                "a non-string intentRef falls back to path instead of crashing",
                {"intentRef": 123},
                "AUDIT.md",
                "AUDIT.md",
            ),
            (
                "an anchor-only intentRef falls back to path rather than an empty canonical",
                {"intentRef": "#line-endings"},
                "AUDIT.md",
                "AUDIT.md",
            ),
        ]
        for label, item, path, want in canonical_cases:
            with self.subTest(label):
                self.assertEqual(audit.intent_ref(item, path)[0], want)


class RegistryCase(unittest.TestCase):
    """spec/checks.json and the check functions audit_repo calls from it."""

    def test_every_registered_check_names_a_function_of_its_scope(self) -> None:
        for check in audit.registered_checks():
            with self.subTest(check["id"]):
                arity = len(inspect.signature(audit.check_function(check)).parameters)
                self.assertEqual(arity, 1 if check["scope"] == "repo" else 2)

    def test_every_check_function_is_registered(self) -> None:
        named = {audit.check_function(c).__name__ for c in audit.registered_checks()}
        defined = {n for n in vars(audit) if n.startswith("check_") and callable(vars(audit)[n])}
        defined -= {"check_function", "check_interface", "check_verbatim"}
        self.assertEqual(defined - named, set())

    def test_suppression_ids_resolve_against_the_registry(self) -> None:
        typed = {"types": ["docs"], "driftNotes": []}
        self.assertTrue(audit.is_suppression_id(typed, "python.directories.declared"))
        self.assertFalse(
            audit.is_suppression_id({"types": ["python"]}, "python.directories.declared")
        )
        self.assertTrue(audit.is_suppression_id({"types": ["python"]}, "filenaming.scripts"))
        self.assertTrue(audit.is_suppression_id({"types": ["python"]}, "files.intent"))
        self.assertFalse(audit.is_suppression_id(typed, "settings.general"))

    def test_a_repo_check_reading_the_baseline_runs_after_it(self) -> None:
        checks = list(audit.registered_checks())
        first_read = next(i for i, c in enumerate(checks) if c["scope"] == "baseline")
        reads = re.compile(r"run\.(content|found|docs)\b|_readme\(run\)")
        readers = []
        for i, check in enumerate(checks):
            if check["scope"] != "repo":
                continue
            if reads.search(inspect.getsource(audit.check_function(check))):
                readers.append(check["id"])
                with self.subTest(check["id"]):
                    self.assertGreater(i, first_read)
        self.assertIn("files.intent", readers)

    def test_only_these_checks_skip_the_hub(self) -> None:
        skipped = {c["id"] for c in audit.registered_checks() if not c["hub"]}
        want = {"sections.undeclared", "carried.template-ref", "tree.verbatim", "hubonly.carried"}
        want.add("files.intent")
        self.assertEqual(skipped, want)

    def test_file_checks_read_their_own_baseline_slots(self) -> None:
        run = audit.Run({"name": "Widget"}, {}, "o/r", "main", {}, None, None)
        run.__dict__["hub"] = False
        run.__dict__["baseline"] = {"AGENTS.md": (None, {"Absent"}, {"Pinned"})}
        run.content["AGENTS.md"] = "## Pinned\n\nacme-hub\n"
        presence = audit.check_sections_presence(run, "AGENTS.md")
        self.assertEqual(len(presence), 1)
        self.assertIn("'Absent'", presence[0][1])
        with mock.patch.object(audit, "check_verbatim", lambda label, *_, **__: [("DRIFT", label)]):
            verbatim = audit.check_sections_verbatim(run, "AGENTS.md")
        self.assertEqual(verbatim, [("DRIFT", "AGENTS.md section 'Pinned'")])
        with mock.patch.object(audit, "hub_repo_name", lambda: "acme-hub"):
            self.assertEqual(audit.check_carried_template_ref(run, "AGENTS.md"), [])

    def test_the_hub_run_skips_only_the_hub_false_checks(self) -> None:
        seen: list[str] = []

        def recorder(check: dict[str, Any]) -> Callable[..., list[Any]]:
            def run_check(run: Any, *path: str) -> list[Any]:
                seen.append(check["id"])
                if check["scope"] == "baseline":
                    run.content[path[0]] = ""
                return []

            return run_check

        head = {"commit": {"sha": "c0ffee", "commit": {"tree": {"sha": "t"}}}}

        def gh(path: str, ok404: bool = False) -> Any:
            return {"private": False} if path == "repos/acme/Hub" else head

        entry = {"name": "Hub", "url": "https://github.com/acme/Hub", "types": ["docs"]}
        spec = {"registry": {"defaults": {}}, "files": {"baseline": [{"path": "AGENTS.md"}]}}
        with (
            mock.patch.object(audit, "gh", gh),
            mock.patch.object(audit, "hub_repo_name", lambda: "Hub"),
            mock.patch.object(audit, "check_function", recorder),
        ):
            audit.audit_repo(entry, spec)
        skipped = {
            "sections.undeclared",
            "carried.template-ref",
            "tree.verbatim",
            "hubonly.carried",
        }
        self.assertEqual(skipped & set(seen), set())
        self.assertIn("carried.version-literal", seen)
        self.assertEqual(seen[-1], "registry.driftnotes")

    def test_an_unparsed_dependabot_file_is_one_finding(self) -> None:
        run = audit.Run({"name": "Widget"}, {}, "o/r", "main", {}, None, None)
        run.head = {"commit": {"sha": "c0ffee"}}
        bad = {"encoding": "base64", "content": base64.b64encode(b"updates:\n  - [\n").decode()}

        def gh(path: str, ok404: bool = False) -> Any:
            return bad if path.endswith("dependabot.yml?ref=c0ffee") else None

        with mock.patch.object(audit, "gh", gh):
            got = audit.check_dependabot_ecosystems(run)
        want = "dependabot: .github/dependabot.yml does not parse as YAML, so its ecosystems are unread"
        self.assertEqual(got, [("DRIFT", want)])

    def test_a_schedule_trigger_reads_in_each_on_shape(self) -> None:
        job = (
            "jobs:\n  publish:\n    uses: acme/hub/.github/workflows/build-release-task.yml@x\n"
            "    if: ${{ github.event_name == 'workflow_dispatch' }}\n"
        )
        for on, want in (
            ("on: [schedule, workflow_dispatch]\n", True),
            ("on: schedule\n", True),
            ("on: [schedule, push]\n", False),
        ):
            with self.subTest(on):
                doc = audit.yaml.safe_load(on + job)
                self.assertEqual(audit._schedule_never_publishes(doc), want)

    def test_the_driftnote_check_runs_last(self) -> None:
        # It counts the findings before it, so a check registered after it would go uncounted.
        self.assertEqual(audit.registered_checks()[-1]["id"], "registry.driftnotes")

    def test_the_smoke_selftest_passes(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(audit._selftest_cases(), 0, out.getvalue())

    def test_a_registry_only_id_reads_as_evaluated_and_a_catalog_id_does_not(self) -> None:
        spec = {"types": {"types": {"python": {"checks": [{"id": "settings.general"}]}}}}
        typed = {"types": ["python"], "driftNotes": ["Owed (settings.general)."]}
        catalog = audit.driftnote_findings(typed, spec, 1)
        self.assertNotIn("this audit evaluates", catalog[0][1])
        registry_only = audit.driftnote_findings(typed, {"types": {"types": {}}}, 1)
        self.assertIn("this audit evaluates (spec/checks.json)", registry_only[0][1])

    def test_an_unparsed_dependabot_file_reads_as_unparsed(self) -> None:
        self.assertIsNone(audit.dependabot_ecosystems("updates:\n  - [\n"))
        self.assertEqual(audit.dependabot_ecosystems("updates: []\n"), set())
        declared = "updates:\n  - package-ecosystem: github-actions\n  - package-ecosystem: 'uv'\n"
        self.assertEqual(audit.dependabot_ecosystems(declared), {"github-actions", "uv"})

    def test_a_registry_id_is_a_known_check_id(self) -> None:
        spec: dict[str, Any] = {"types": {"types": {}, "crossCutting": {}}}
        self.assertEqual(audit.catalog_owner(spec, "settings.general"), (None, False))
        self.assertEqual(audit.registered_check("settings.general")["id"], "settings.general")
        self.assertEqual(audit.registered_check("no.such-check"), {})


class ExpectedRulesetsCase(unittest.TestCase):
    """The audit expects the rulesets repo-config/configure.sh composes, through the same function."""

    @staticmethod
    def types(entry: dict[str, Any], model: str) -> dict[str, list[str]]:
        _, expect = audit.expected_rulesets({"repos": [entry]}, entry, model)
        return {name: [r["type"] for r in payload["rules"]] for name, payload in expect.items()}

    def test_a_declared_false_drops_the_check_from_both(self) -> None:
        kept = self.types({"name": "Fixture"}, "release")
        dropped = self.types({"name": "Fixture", "pullRequestCheck": False}, "release")
        for name in ("develop", "main"):
            with self.subTest(ruleset=name):
                self.assertIn("required_status_checks", kept[name])
                self.assertEqual(
                    dropped[name], [t for t in kept[name] if t != "required_status_checks"]
                )

    def test_a_policy_with_no_automatic_copilot_expects_no_copilot_rule(self) -> None:
        bots = {"copilot": {"trigger": "explicit"}}
        entry = {"name": "Fixture", "agentPolicy": {"review": {"bots": bots}}}
        self.assertNotIn("copilot_code_review", self.types(entry, "release")["main"])
        self.assertIn("copilot_code_review", self.types({"name": "Fixture"}, "release")["main"])

    def test_each_source_payload_is_named(self) -> None:
        sources, _ = audit.expected_rulesets({"repos": []}, {"name": "Fixture"}, "operational")
        self.assertEqual(sources["develop"], "repo-config/operational/develop.json")
        self.assertEqual(sources["main"], "repo-config/main.json")


if __name__ == "__main__":
    unittest.main()
