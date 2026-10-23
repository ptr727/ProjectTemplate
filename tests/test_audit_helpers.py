#!/usr/bin/env python3
"""spec/audit.py's helpers on constructed fixtures, with no network read."""

from __future__ import annotations

import contextlib
import io
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
from git_env import git_location_cleared

_LOCATION = contextlib.ExitStack()


def setUpModule() -> None:
    # A git hook sets GIT_DIR, which would otherwise place every temporary repository inside the hook's own.
    _LOCATION.enter_context(git_location_cleared())


def tearDownModule() -> None:
    _LOCATION.close()


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


class SplitJobsCase(unittest.TestCase):
    """Holds the split_jobs edge cases."""

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
            if not audit._IS_WINDOWS:
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
            mock.patch.object(audit, "_IS_WINDOWS", windows),
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


GIT_IDENTITY = (
    ["git", "init", "-q"],
    ["git", "config", "user.email", "test@test.invalid"],
    ["git", "config", "user.name", "test"],
    ["git", "config", "commit.gpgsign", "false"],
)


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
                self.assertEqual(audit.unfenced_text(doc), want)

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
                self.assertEqual(audit.undeclared_h2_headings(doc, declared), want)


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
        self.assertIn("(3 findings)", it, msg)
        self.assertIn("## Must fix", ib, msg)
        self.assertIn("**LETTER** file: X absent", ib, msg)
        self.assertIn("## Converge", ib, msg)
        self.assertIn("verbatim: Y differs", ib, msg)
        self.assertIn("## Could not verify", ib, msg)
        self.assertIn("(0 findings)", et, msg)
        self.assertIn("nothing to converge", eb, msg)

    def test_title_and_intro_mirror(self) -> None:
        r_md = "# Widget <!-- omit from toc -->\n\nDoes widget things.\n\n## Build\n"
        h_md = "# Widget\n\nDoes widget things.\n\n## Release History\n"
        h_bad = "# Widget\n\nDoes other things.\n\n## Release History\n"
        r_two = "# W\n\nLine one.\n\nLine two.\n\n## Build\n"
        h_joined = "# W\n\nLine one.\nLine two.\n\n## Release History\n"
        msg = "history: README/HISTORY title+intro mirror detection"
        self.assertEqual(audit.title_and_intro(r_md), audit.title_and_intro(h_md), msg)
        self.assertNotEqual(audit.title_and_intro(r_md), audit.title_and_intro(h_bad), msg)
        self.assertNotEqual(audit.title_and_intro(r_two), audit.title_and_intro(h_joined), msg)

    def test_strip_md_links(self) -> None:
        linked = "Utility to clean [media](https://x.example/Foo_(bar)) per the [spec][spec-ref]."
        nested = "See [API [docs]](https://example.test/a_(b)_(c)) for details."
        inner_link = "See [[docs](url)] for details."
        escaped_bracket = r"See [API \[docs](url) for details."
        msg = "description: strip_md_links behavior"
        refs = audit.references("x\n\n[spec-ref]: https://example.test\n")
        self.assertEqual(
            audit.strip_md_links(linked, refs), "Utility to clean media per the spec.", msg
        )
        self.assertEqual(
            audit.strip_md_links(linked), "Utility to clean media per the [spec][spec-ref].", msg
        )
        self.assertEqual(audit.strip_md_links("Plain intro line."), "Plain intro line.", msg)
        self.assertEqual(audit.strip_md_links(nested), "See API [docs] for details.", msg)
        self.assertEqual(audit.strip_md_links(inner_link), "See [docs] for details.", msg)
        self.assertEqual(audit.strip_md_links(escaped_bracket), r"See API \[docs for details.", msg)

    def test_strip_md_links_stays_linear(self) -> None:
        pathological = "[" * 20000
        start = time.monotonic()
        audit.strip_md_links(pathological)
        elapsed = time.monotonic() - start
        self.assertLessEqual(
            elapsed,
            1.0,
            f"description: strip_md_links took {elapsed:.2f}s on unmatched brackets, expected linear",
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


class ShallowCloneCase(unittest.TestCase):
    """Holds the shallow-clone refusal on the history reads and on the public paths."""

    def make_dir(self) -> pathlib.Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name)

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
        ["does not define"],
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

DOCKER_SHIELD = "[![Docker Latest][e]][x]\n[e]: https://img.shields.io/docker/v/o/r/latest\n"

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
        ),
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
    # The sample sits after the real definition deliberately, so reading across fences would override it.
    (
        "a fenced sample does not supply a tool link",
        TOOLS_HEAD
        + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
        + TOOLS_DEFS
        + "\n```md\n[cspell-link]: https://wrong.example/\n```\n",
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


ALT_CASES = [
    (("codecov.yml",), ("codecov.yml", "codecov.yml"), "only the declared path"),
    ((".codecov.yml",), (".codecov.yml", ".codecov.yml"), "only the alternative"),
    (("codecov.yml", ".codecov.yml"), ("codecov.yml", "codecov.yml"), "both, declared wins"),
    ((), ("codecov.yml", None), "neither"),
]
BASELINE_UNIT = {"path": "codecov.yml", "fidelity": "intent", "intentRef": "WORKFLOW.md"}


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
                got = audit.readme_section_findings(text, self.rm, sel_types, public)
                self.assertEqual(len(got), wantn, f"readme sections: {label} -> {got}")

    def test_shield_cases(self) -> None:
        for case in SHIELD_CASES:
            label, text, ent, wantn = case[:4]
            want_text = case[4] if len(case) > 4 else None
            with self.subTest(label):
                got = audit.readme_shield_findings(text, self.rm, ent)
                good = len(got) == wantn and (
                    want_text is None or any(want_text in t for _, t in got)
                )
                self.assertTrue(good, f"readme shields: {label} want={wantn} {want_text} -> {got}")

    def test_link_cases(self) -> None:
        for label, text, want_letter, want_drift in LINK_CASES:
            with self.subTest(label):
                got = audit.readme_link_findings(text, self.rm, "o/r")
                gl = sum(1 for k, _ in got if k == "LETTER")
                gd = sum(1 for k, _ in got if k == "DRIFT")
                self.assertEqual(
                    (gl, gd), (want_letter, want_drift), f"readme links: {label} {got}"
                )

    def test_tool_cases(self) -> None:
        cat = audit.load("spec/third-party-tools.json")
        for label, text, wantn in TOOL_CASES:
            with self.subTest(label):
                got = audit.third_party_tool_findings(text, cat)
                self.assertEqual(len(got), wantn, f"3rd party tools: {label} -> {got}")

    def test_canonical_link_entry_with_neither_repopath_nor_match_raises(self) -> None:
        broken = dict(
            self.rm, canonicalLinks=list(self.rm["canonicalLinks"]) + [{"name": "orphan-link"}]
        )
        with self.assertRaises(
            KeyError,
            msg="canonicalLinks entry with neither repoPath nor match: no error raised",
        ) as ctx:
            audit.canonical_link_entry("https://example.test/", broken, "o/r")
        message = str(ctx.exception)
        self.assertTrue(
            "readme-sections.json" in message and "orphan-link" in message,
            f"canonicalLinks entry error does not name the file and entry -> {ctx.exception}",
        )

    def test_tagline_is_the_first_line_of_the_intro_region(self) -> None:
        two_para = audit.title_and_intro(
            "# X\n\nThe tagline.\n\nA clarifying paragraph.\n\n## Next\n"
        )[1]
        self.assertEqual(audit.tagline(two_para), "The tagline.")
        self.assertNotIn("\n", audit.tagline(two_para))


class DescriptionCase(unittest.TestCase):
    """description_findings(): the README, About, and Docker Hub mirror set."""

    def test_description_cases(self) -> None:
        for label, doc_texts_fx, entry_fx, live_fx, wantn in DESC_CASES:
            with self.subTest(label):
                got = audit.description_findings(doc_texts_fx, entry_fx, live_fx, "owner/Fixture")
                self.assertEqual(len(got), wantn, f"description: {label} -> {got}")

    def test_null_declared_field_is_a_defect_via_validates_contract(self) -> None:
        null_declared = audit.description_findings(
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
        null_declared = audit.description_findings(
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
        declared_mismatch = audit.description_findings(
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
            audit, "docker_hub_description", lambda slug: "A stale Docker Hub blurb."
        ):
            docker_mismatch = audit.description_findings(
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

    def test_read_baseline_file_alternatives(self) -> None:
        for held, (want_path, want_name), label in ALT_CASES:
            with self.subTest(label):
                with mock.patch.object(audit, "gh", holding(*held)):
                    got_path, got = audit.read_baseline_file(
                        "o/r", "main", "codecov.yml", [".codecov.yml"]
                    )
                got_name = got["name"] if got is not None else None
                self.assertEqual(
                    (got_path, got_name),
                    (want_path, want_name),
                    f"read_baseline_file, {label} -> {got_path}, {got_name}",
                )

    def test_baseline_file_findings_only_the_alternative(self) -> None:
        with (
            mock.patch.object(
                audit, "hub_last_change", lambda rel: ("2020-01-01T00:00:00+00:00", "abc1234")
            ),
            mock.patch.object(audit, "canonical_current_text", lambda rel: "canonical"),
            mock.patch.object(audit, "gh", holding(".codecov.yml")),
        ):
            alt_content, _, alt_findings = audit.baseline_file_findings(
                "o/r", "main", "codecov.yml", [".codecov.yml"], BASELINE_UNIT, False
            )
        stale_at_alt = [t for lvl, t in alt_findings if lvl == "DRIFT" and ".codecov.yml" in t]
        self.assertTrue(
            alt_content is not None and len(alt_findings) == 1 and len(stale_at_alt) == 1,
            f"baseline_file_findings, only the alternative -> {alt_findings}",
        )

    def test_baseline_file_findings_neither(self) -> None:
        with (
            mock.patch.object(
                audit, "hub_last_change", lambda rel: ("2020-01-01T00:00:00+00:00", "abc1234")
            ),
            mock.patch.object(audit, "canonical_current_text", lambda rel: "canonical"),
            mock.patch.object(audit, "gh", holding()),
        ):
            _, _, none_findings = audit.baseline_file_findings(
                "o/r", "main", "codecov.yml", [".codecov.yml"], BASELINE_UNIT, False
            )
        want_none = [
            (
                "LETTER",
                "file: codecov.yml or .codecov.yml absent on main (verify intent per AUDIT.md section 7)",
            )
        ]
        self.assertEqual(
            none_findings, want_none, f"baseline_file_findings, neither -> {none_findings}"
        )

    def test_declared_alternatives_on_the_live_manifest(self) -> None:
        live_alts = audit.declared_alternatives(audit.load("spec/files.json")["baseline"])
        self.assertEqual(
            live_alts.get("codecov.yml"),
            [".codecov.yml"],
            f"declared_alternatives on the live manifest -> {live_alts}",
        )


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
    """Holds the runtime secret file stem findings."""

    def test_secret_stem_cases(self) -> None:
        secret_stem_cases = [
            (
                "conforming stems, examples, and the directory's own files pass",
                {
                    ".secrets/README.md",
                    ".secrets/.gitignore",
                    ".secrets/db_root_password.example",
                    ".secrets/mail_relay.env.example",
                    "deploy/.secrets/service_account.json.example",
                    "docs/Mixed-Case.md",
                },
                set(),
            ),
            (
                "a hyphenated, capitalized, or bare stem breaks",
                {
                    ".secrets/db-root-password.example",
                    ".secrets/Mail_Relay.env.example",
                    ".secrets/.env.example",
                    "deploy/.secrets/api-token",
                },
                {
                    ".secrets/db-root-password.example",
                    ".secrets/Mail_Relay.env.example",
                    ".secrets/.env.example",
                    "deploy/.secrets/api-token",
                },
            ),
            (
                "a file outside a .secrets directory, or nested below one, is out of scope",
                {
                    "secrets/db-root-password.example",
                    "config/.secrets-notes/Odd-Name",
                    ".secrets/docs/Mixed-Case.md",
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
                    and got[0][1].startswith(f"runtime-secrets: {len(want)} secret file(s)")
                    and all(p in got[0][1] for p in want)
                )
                self.assertTrue(good, f"{got}")

    def test_unreadable_tree_reports_nothing(self) -> None:
        self.assertEqual(
            audit.secret_stem_findings(None), [], "an unreadable tree reports nothing here"
        )


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
    """Holds the intent_canonical_rel resolution check."""

    def test_intent_canonical_rel(self) -> None:
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
                {"reference": "catalog/snippets/configs/codecov.yml", "intentRef": "WORKFLOW.md"},
                "codecov.yml",
                "catalog/snippets/configs/codecov.yml",
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
                self.assertEqual(audit.intent_canonical_rel(item, path), want)


if __name__ == "__main__":
    unittest.main()
