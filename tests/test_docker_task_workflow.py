#!/usr/bin/env python3
"""Pin the Docker task workflow's login-before-push order and its hook inputs.

Every job that can push to Docker Hub must check the credentials and log in before the first step
that pushes, the caller's hook step included, and the hook step must receive the inputs it builds
from. The job and step sets are read from the workflow, so a reordered, renamed, or added job is
judged by the same rule rather than by a hard-coded index.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/build-docker-task.yml"

LOGIN_ACTION = "docker/login-action@"
HOOK_INPUTS = ("push", "ref", "branch", "platforms")
CREDENTIAL_NAMES = ("DOCKER_HUB_USERNAME", "DOCKER_HUB_ACCESS_TOKEN")


class Step(NamedTuple):
    """One step: its name, `uses` value, `with` keys, and raw text."""

    name: str
    uses: str
    with_keys: tuple[str, ...]
    text: str


def job_texts(workflow: str) -> dict[str, str]:
    """Each job's text keyed by job id, read by indentation, since the suite runs on the standard library alone."""
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if re.fullmatch(r"jobs:\s*(#.*)?", line))
    jobs: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines[start + 1 :]:
        header = re.fullmatch(r"  ([A-Za-z0-9_-]+):\s*(#.*)?", line)
        if header:
            current = jobs.setdefault(header.group(1), [])
        elif line.strip() and not line.startswith(" "):
            break
        if current is not None:
            current.append(line)
    return {job: "\n".join(body) for job, body in jobs.items()}


def job_steps(job: str) -> list[Step]:
    """The job's steps in order, each bounded to its own sequence item."""
    items: list[list[str]] = []
    in_steps = False
    for line in job.splitlines():
        if re.fullmatch(r"    steps:\s*(#.*)?", line):
            in_steps = True
        elif in_steps and line.startswith("      - "):
            items.append([line])
        elif in_steps and items and (not line.strip() or line.startswith("        ")):
            items[-1].append(line)
        elif in_steps and line.strip() and not line.lstrip().startswith("#"):
            break
    steps = []
    for item in items:
        text = "\n".join(item)
        name = re.search(r"(?m)^      - name: (.+)$", text)
        uses = re.search(r"(?m)^        uses: [\"']?([^\s\"']+)", text)
        steps.append(
            Step(
                name.group(1) if name else "",
                uses.group(1) if uses else "",
                with_keys(item),
                text,
            )
        )
    return steps


def with_keys(item: list[str]) -> tuple[str, ...]:
    """The keys directly under the step's `with:`, ignoring comments."""
    keys: list[str] = []
    in_with = False
    for line in item:
        if re.fullmatch(r"        with:\s*(#.*)?", line):
            in_with = True
        elif in_with:
            key = re.match(r"          ([A-Za-z0-9_-]+):", line)
            if key:
                keys.append(key.group(1))
            elif line.strip() and not line.startswith("          "):
                break
    return tuple(keys)


def ungated(step: Step) -> bool:
    """The step has no `if` or `continue-on-error`, so it always runs and a failure stops the job."""
    return not re.search(r"(?m)^        (?:if|continue-on-error):", step.text)


def is_credential_check(step: Step) -> bool:
    """A run step reading both Docker Hub secrets through env and testing them empty."""
    return (
        not step.uses
        and "        run: |" in step.text
        and all(f"{name}: ${{{{ secrets.{name} }}}}" in step.text for name in CREDENTIAL_NAMES)
        and all(f'-z "${name}"' in step.text for name in CREDENTIAL_NAMES)
        and "exit 1" in step.text
        and ungated(step)
    )


def is_login(step: Step) -> bool:
    """The Docker login action fed both secrets."""
    return (
        step.uses.startswith(LOGIN_ACTION)
        and all(f"secrets.{name}" in step.text for name in CREDENTIAL_NAMES)
        and ungated(step)
    )


def can_push(step: Step) -> bool:
    """A step that can push: one passing `push`, or a caller build hook even if its push is lost."""
    is_build_hook = step.uses.startswith("./.github/actions/") and "build" in step.uses
    return "push" in step.with_keys or is_build_hook


def violations(workflow: str) -> list[str]:
    """Every way the workflow breaks the login-before-push order or the hook inputs."""
    workflow = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    found: list[str] = []
    pushing = 0
    for job_id, body in job_texts(workflow).items():
        steps = job_steps(body)
        pushes = [i for i, step in enumerate(steps) if can_push(step)]
        if not pushes:
            continue
        pushing += 1
        first_push = pushes[0]
        checks = [i for i, step in enumerate(steps) if is_credential_check(step)]
        logins = [i for i, step in enumerate(steps) if is_login(step)]
        if not checks or checks[0] > first_push:
            found.append(f"{job_id}: no credential check before the first push step")
        if not logins or logins[0] > first_push:
            found.append(f"{job_id}: no login before the first push step")
        if checks and logins and checks[0] > logins[0]:
            found.append(f"{job_id}: login before the credential check")
        for step in steps:
            if can_push(step) and step.uses.startswith("./.github/actions/"):
                missing = [key for key in HOOK_INPUTS if key not in step.with_keys]
                if missing:
                    found.append(f"{job_id}: hook {step.uses} lacks {', '.join(missing)}")
                for key in ("push", "ref", "branch"):
                    if f"          {key}: ${{{{ inputs.{key} }}}}" not in step.text:
                        found.append(f"{job_id}: hook {step.uses} does not forward inputs.{key}")
    if pushing < 2:
        found.append(f"expected at least two pushing jobs, found {pushing}")
    return found


STEP_END = r"(?=^      (?:- |#)|^  \S|\Z)"


def step_text(body: str, name: str) -> str:
    """The raw text of the one step of that name in a job body."""
    matches = re.findall(rf"(?ms)^      - name: {re.escape(name)}\n.*?{STEP_END}", body)
    assert len(matches) == 1, name
    return matches[0]


def drop_step(workflow: str, name: str) -> str:
    """The workflow text without every step of that name."""
    return re.sub(rf"(?ms)^      - name: {re.escape(name)}\n.*?{STEP_END}", "", workflow)


def move_step_after(workflow: str, job: str, name: str, anchor: str) -> str:
    """Move the step `name` to directly after the step `anchor`, both within one job."""
    body = job_texts(workflow)[job]
    moved = step_text(body, name)
    cut = body.replace(moved, "", 1)
    anchor_text = step_text(cut, anchor)
    end = cut.index(anchor_text) + len(anchor_text)
    head = cut[:end] if cut[:end].endswith("\n") else cut[:end] + "\n"
    return workflow.replace(body, head + moved + cut[end:], 1)


class DockerTaskWorkflowCase(unittest.TestCase):
    """The checked-in workflow passes, and each mutation of it is caught."""

    workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_is_clean(self) -> None:
        self.assertEqual([], violations(self.workflow))

    def test_both_known_jobs_are_judged(self) -> None:
        pushing = {
            job_id
            for job_id, body in job_texts(self.workflow).items()
            if any(can_push(step) for step in job_steps(body))
        }
        self.assertTrue({"build-docker", "docker-build-base"} <= pushing)

    def test_dropped_login_is_caught(self) -> None:
        mutated = drop_step(self.workflow, "Login to Docker Hub step")
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn("docker-build-base: no login before the first push step", violations(mutated))
        self.assertIn("build-docker: no login before the first push step", violations(mutated))

    def test_dropped_credential_check_is_caught(self) -> None:
        mutated = drop_step(self.workflow, "Require Docker Hub credentials step")
        self.assertNotEqual(self.workflow, mutated)
        found = violations(mutated)
        self.assertIn("docker-build-base: no credential check before the first push step", found)
        self.assertIn("build-docker: no credential check before the first push step", found)

    def test_login_moved_after_the_hook_is_caught(self) -> None:
        hook = "Run caller docker-build-base hook step"
        mutated = move_step_after(
            self.workflow, "docker-build-base", "Login to Docker Hub step", hook
        )
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn("docker-build-base: no login before the first push step", violations(mutated))

    def test_login_moved_after_the_build_is_caught(self) -> None:
        build = "Docker build and push step"
        mutated = move_step_after(self.workflow, "build-docker", "Login to Docker Hub step", build)
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn("build-docker: no login before the first push step", violations(mutated))

    def test_check_moved_after_the_login_is_caught(self) -> None:
        mutated = move_step_after(
            self.workflow,
            "docker-build-base",
            "Require Docker Hub credentials step",
            "Login to Docker Hub step",
        )
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn("docker-build-base: login before the credential check", violations(mutated))

    def test_gated_check_and_login_are_caught(self) -> None:
        for name, flag in (
            ("Require Docker Hub credentials step", "continue-on-error: true"),
            ("Login to Docker Hub step", "if: ${{ inputs.push }}"),
        ):
            with self.subTest(name=name):
                body = job_texts(self.workflow)["build-docker"]
                step = step_text(body, name)
                gated = step.replace("\n        ", f"\n        {flag}\n        ", 1)
                mutated = self.workflow.replace(body, body.replace(step, gated, 1), 1)
                self.assertNotEqual(self.workflow, mutated)
                found = violations(mutated)
                self.assertTrue(any(v.startswith("build-docker: no ") for v in found))

    def test_half_a_credential_check_is_caught(self) -> None:
        mutated = self.workflow.replace(' || -z "$DOCKER_HUB_ACCESS_TOKEN"', "", 1)
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn(
            "docker-build-base: no credential check before the first push step", violations(mutated)
        )

    def test_miswired_hook_input_is_caught(self) -> None:
        mutated = self.workflow.replace(
            "          ref: ${{ inputs.ref }}\n          branch:",
            "          ref: ${{ inputs.branch }}\n          branch:",
            1,
        )
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn(
            "docker-build-base: hook ./.github/actions/docker-build-base does not forward inputs.ref",
            violations(mutated),
        )

    def test_commented_out_secret_is_caught(self) -> None:
        mutated = self.workflow.replace(
            "          password: ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}",
            "          # password: ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}",
        )
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn("no login before the first push step", "\n".join(violations(mutated)))

    def test_each_dropped_hook_input_is_caught(self) -> None:
        body = job_texts(self.workflow)["docker-build-base"]
        hook = step_text(body, "Run caller docker-build-base hook step")
        for key in HOOK_INPUTS:
            with self.subTest(key=key):
                lines = [
                    line
                    for line in hook.splitlines(keepends=True)
                    if not line.startswith(f"          {key}:")
                ]
                mutated = self.workflow.replace(hook, "".join(lines), 1)
                self.assertNotEqual(self.workflow, mutated)
                self.assertTrue(any(f"lacks {key}" in v for v in violations(mutated)))


if __name__ == "__main__":
    unittest.main()
