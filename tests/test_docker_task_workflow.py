#!/usr/bin/env python3
"""Pin the Docker task workflow's login-before-push order and its hook inputs.

Every job that can push to Docker Hub must check the credentials and log in before the first step
that pushes, the caller's hook step included, and the hook step must receive the inputs it builds
from. The job and step sets are read from the workflow, so a reordered, renamed, or added job is
judged by the same rule rather than by a hard-coded index.

The reader is an indentation reader on the standard library alone, not a YAML or shell parser.
Its remaining limits are pinned by the `KnownLimitsCase` cases below, so a change that alters one is noticed.
A push inside a heredoc body, a quoted string, or a trailing shell comment is read as a push.
A `type=registry` cache source is read as a push.
A command folded across lines without a backslash is not joined.
A flow-style `with` mapping spanning lines is not read.
A block-scalar login value is not read as the secret.
An inverted credential check is accepted, and the secret names and `-z` tests are matched anywhere in the step.
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
    """One step: its name, `uses` value, `with` keys and values, and raw text."""

    name: str
    uses: str
    with_keys: tuple[str, ...]
    text: str
    with_values: dict[str, str]


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


def split_flow(body: str) -> list[str]:
    """Split a flow mapping body on its top-level commas, keeping `${{ }}` expressions whole."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, char in enumerate(body):
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return [part.strip() for part in parts if part.strip()]


def normalize_item(item: list[str]) -> list[str]:
    """Re-indent one step to the block layout the readers expect and expand a one-line `with` mapping."""
    first = re.fullmatch(r"(\s*)- +((?!name:)[A-Za-z0-9_-]+:.*)", item[0])
    if first:
        item = [f"{first.group(1)}-", f"{first.group(1)}  {first.group(2)}", *item[1:]]
    shift = 6 - (len(item[0]) - len(item[0].lstrip()))
    lines = [
        (" " * shift + line if shift >= 0 else line[min(-shift, len(line) - len(line.lstrip())) :])
        if line.strip()
        else line
        for line in item
    ]
    expanded: list[str] = []
    for line in lines:
        flow = re.fullmatch(r"        with:\s*\{(.*)\}\s*(#.*)?", line)
        if flow:
            expanded.append("        with:")
            expanded.extend(f"          {pair}" for pair in split_flow(flow.group(1)))
        else:
            expanded.append(line)
    return expanded


def job_steps(job: str) -> list[Step]:
    """The job's steps in order, each bounded to its own sequence item, compact or indented."""
    items: list[list[str]] = []
    steps_indent = -1
    item_indent = -1
    for line in job.splitlines():
        indent = len(line) - len(line.lstrip())
        if steps_indent < 0:
            if re.fullmatch(r"    steps:\s*(#.*)?", line):
                steps_indent = indent
        elif (
            line.lstrip().startswith("- ")
            and indent >= steps_indent
            and item_indent in (-1, indent)
        ):
            item_indent = indent
            items.append([line])
        elif items and (not line.strip() or indent > item_indent):
            items[-1].append(line)
        elif line.strip() and not line.lstrip().startswith("#"):
            break
    steps = []
    for raw in items:
        item = normalize_item(raw)
        text = "\n".join(item)
        name = re.search(r"(?m)^      - name: (.+)$", text)
        uses = re.search(r"(?m)^        uses: [\"']?([^\s\"']+)", text)
        values = with_values(item)
        steps.append(
            Step(
                name.group(1) if name else "",
                uses.group(1) if uses else "",
                tuple(values),
                text,
                values,
            )
        )
    return steps


def with_values(item: list[str]) -> dict[str, str]:
    """The keys and values directly under the step's `with:`, ignoring comments."""
    values: dict[str, str] = {}
    in_with = False
    child = ""
    for line in item:
        if re.fullmatch(r"        with:\s*(#.*)?", line):
            in_with = True
        elif in_with:
            if not child and line.strip() and not line.lstrip().startswith("#"):
                child = line[: len(line) - len(line.lstrip())]
                if len(child) <= 8:
                    break
            pair = (
                re.match(rf"{child}[\"']?([A-Za-z0-9_-]+)[\"']?:\s*(.*?)\s*$", line)
                if child
                else None
            )
            if pair:
                value = re.sub(r"\s+#.*$", "", pair.group(2)).strip("\"'")
                values[pair.group(1)] = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", r"${{ \1 }}", value)
            elif line.strip() and not line.startswith(child):
                break
    return values


def ungated(step: Step) -> bool:
    """The step has no `if` or `continue-on-error`, so it always runs and a failure stops the job."""
    return not re.search(r"(?m)^        (?:if|continue-on-error):", step.text)


def empty_tests_joined_by_or(text: str) -> bool:
    """The check tests the two secrets empty in one condition joined by `||`, in either order."""
    first, second = (rf'-z "\${name}"' for name in CREDENTIAL_NAMES)
    return bool(re.search(rf"{first}\s*\|\|\s*{second}|{second}\s*\|\|\s*{first}", text))


def is_credential_check(step: Step) -> bool:
    """A run step reading both Docker Hub secrets through env and testing them empty."""
    return (
        not step.uses
        and "        run: |" in step.text
        and all(f"{name}: ${{{{ secrets.{name} }}}}" in step.text for name in CREDENTIAL_NAMES)
        and empty_tests_joined_by_or(step.text)
        and "exit 1" in step.text
        and ungated(step)
    )


def is_login(step: Step) -> bool:
    """The Docker login action fed each secret to its own input, against the default registry."""
    secrets = dict(zip(("username", "password"), CREDENTIAL_NAMES, strict=True))
    return (
        step.uses.startswith(LOGIN_ACTION)
        and all(
            step.with_values.get(key) == f"${{{{ secrets.{name} }}}}"
            for key, name in secrets.items()
        )
        and step.with_values.get("registry", "docker.io") in ("docker.io", "index.docker.io", "")
        and ungated(step)
    )


DOCKER = r"\bdocker(?:\s+--?[A-Za-z][\w-]*(?:=\S+|\s+[^-\s]\S*)?)*?"
PUSH_COMMAND = re.compile(
    rf"{DOCKER}\s+(?:(?:image|manifest|buildx)\s+)?push\b"
    rf"|{DOCKER}\s+(?:buildx\s+)?(?:build|bake)\b.*(?:--push\b(?!=(?:false|0)\b)|type=registry\b)"
)


def run_script(step: Step) -> str:
    """The step's `run` value with line continuations joined, or empty for a step without one."""
    lines = step.text.splitlines()
    for i, line in enumerate(lines):
        match = re.fullmatch(r"        run:\s*(.*)", line)
        if match:
            body = [match.group(1)]
            for rest in lines[i + 1 :]:
                if rest.strip() and not rest.startswith("          "):
                    break
                body.append(rest)
            return re.sub(r"\\\n\s*", " ", "\n".join(body))
    return ""


def runs_push(step: Step) -> bool:
    """A run command pushes, where a command that only echoes the words does not."""
    for command in re.split(r"&&|\|\||[;|\n]", run_script(step)):
        words = command.strip().lstrip("|>-+ ").split(maxsplit=1)
        if words and words[0] not in ("echo", "printf") and PUSH_COMMAND.search(command):
            return True
    return False


def can_push(step: Step) -> bool:
    """A step that can push: a `with` push input, a run command that pushes, or a caller build hook."""
    is_build_hook = step.uses.startswith("./.github/actions/") and "build" in step.uses
    pushes_from_run = not step.uses and runs_push(step)
    return "push" in step.with_keys or is_build_hook or pushes_from_run


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
                    if step.with_values.get(key) != f"${{{{ inputs.{key} }}}}":
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


def compact(workflow: str) -> str:
    """The workflow with each job's step sequence written at the same indent as `steps:`."""
    out: list[str] = []
    in_steps = False
    for line in workflow.splitlines():
        if re.fullmatch(r"    steps:\s*(#.*)?", line):
            in_steps = True
        elif re.match(r"  \S", line):
            in_steps = False
        elif in_steps and line.startswith("  "):
            line = line[2:]
        out.append(line)
    return "\n".join(out) + "\n"


def flowify_with(workflow: str, name: str) -> str:
    """The workflow with the named step's `with` block written as a one-line flow mapping."""
    step = step_text(workflow, name)
    pairs = re.findall(r"(?m)^          ([A-Za-z0-9_-]+): (.+)$", step)
    block = re.search(r"(?ms)^        with:\n(?:          .*\n?)+", step)
    assert block
    flow = ", ".join(f"{key}: {value}" for key, value in pairs)
    return workflow.replace(step, step.replace(block.group(0), f"        with: {{{flow}}}\n"), 1)


def insert_before_login(workflow: str, job: str, step: str) -> str:
    """The workflow with a step written before the login step of one job."""
    body = job_texts(workflow)[job]
    login = step_text(body, "Login to Docker Hub step")
    return workflow.replace(body, body.replace(login, step + login, 1), 1)


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

    def test_and_joined_credential_check_is_caught(self) -> None:
        joined = '-z "$DOCKER_HUB_USERNAME" || -z "$DOCKER_HUB_ACCESS_TOKEN"'
        self.assertIn(joined, self.workflow)
        mutated = self.workflow.replace(joined, joined.replace("||", "&&"))
        self.assertNotEqual(self.workflow, mutated)
        found = violations(mutated)
        self.assertIn("docker-build-base: no credential check before the first push step", found)
        self.assertIn("build-docker: no credential check before the first push step", found)

    def test_reversed_credential_check_is_clean(self) -> None:
        joined = '-z "$DOCKER_HUB_USERNAME" || -z "$DOCKER_HUB_ACCESS_TOKEN"'
        mutated = self.workflow.replace(
            joined, '-z "$DOCKER_HUB_ACCESS_TOKEN" || -z "$DOCKER_HUB_USERNAME"'
        )
        self.assertNotEqual(self.workflow, mutated)
        self.assertEqual([], violations(mutated))

    def test_miswired_login_inputs_are_caught(self) -> None:
        user = "          username: ${{ secrets.DOCKER_HUB_USERNAME }}"
        password = "          password: ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}"
        swapped = self.workflow.replace(user, "@@").replace(
            password, user.replace("username", "password")
        )
        swapped = swapped.replace("@@", password.replace("password", "username"))
        for label, mutated in (
            ("swapped inputs", swapped),
            (
                "other registry",
                self.workflow.replace(
                    password, password + "\n          registry: registry.example.com"
                ),
            ),
        ):
            with self.subTest(label=label):
                self.assertNotEqual(self.workflow, mutated)
                found = violations(mutated)
                self.assertIn("build-docker: no login before the first push step", found)

    def test_compact_steps_are_parsed(self) -> None:
        mutated = compact(self.workflow)
        self.assertNotEqual(self.workflow, mutated)
        self.assertEqual([], violations(mutated))
        dropped = compact(drop_step(self.workflow, "Login to Docker Hub step"))
        self.assertIn("build-docker: no login before the first push step", violations(dropped))

    def test_flow_style_with_is_parsed(self) -> None:
        hook = "Run caller docker-build-base hook step"
        mutated = flowify_with(self.workflow, hook)
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn("with: {push: ${{ inputs.push }}", mutated)
        self.assertEqual([], violations(mutated))
        lacking = mutated.replace(" ref: ${{ inputs.ref }},", "", 1)
        self.assertNotEqual(mutated, lacking)
        self.assertIn(
            "docker-build-base: hook ./.github/actions/docker-build-base lacks ref",
            violations(lacking),
        )

    def test_flow_style_push_before_login_is_caught(self) -> None:
        early = "      - name: Early push step\n        uses: example/push@v1\n        with: {push: true}\n\n"
        mutated = insert_before_login(self.workflow, "build-docker", early)
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn("build-docker: no login before the first push step", violations(mutated))

    def test_run_step_push_before_login_is_caught(self) -> None:
        for command in (
            "docker push example/image:tag",
            "docker buildx build --push .",
            "echo start && docker push example/image:tag",
            "docker manifest push example/image:tag",
            "docker buildx bake --push",
        ):
            with self.subTest(command=command):
                early = f"      - name: Early push step\n        run: {command}\n\n"
                mutated = insert_before_login(self.workflow, "build-docker", early)
                self.assertNotEqual(self.workflow, mutated)
                self.assertIn(
                    "build-docker: no login before the first push step", violations(mutated)
                )

    def test_run_step_that_only_mentions_push_is_not_a_push(self) -> None:
        quiet = '      - name: Quiet step\n        run: echo "docker push later"\n\n'
        mutated = insert_before_login(self.workflow, "build-docker", quiet)
        self.assertNotEqual(self.workflow, mutated)
        self.assertEqual([], violations(mutated))

    def test_continued_run_push_before_login_is_caught(self) -> None:
        early = (
            "      - name: Early push step\n        run: |\n"
            "          docker buildx build \\\n            --push .\n\n"
        )
        mutated = insert_before_login(self.workflow, "build-docker", early)
        self.assertNotEqual(self.workflow, mutated)
        self.assertIn("build-docker: no login before the first push step", violations(mutated))

    def test_step_name_mentioning_push_is_not_a_push(self) -> None:
        quiet = "      - name: Verify docker push prerequisites step\n        run: true\n\n"
        mutated = insert_before_login(self.workflow, "build-docker", quiet)
        self.assertNotEqual(self.workflow, mutated)
        self.assertEqual([], violations(mutated))

    def test_login_value_comment_and_default_registry_are_clean(self) -> None:
        password = "          password: ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}"
        mutated = self.workflow.replace(
            password, password + " # token\n          registry: 'index.docker.io'"
        )
        self.assertNotEqual(self.workflow, mutated)
        self.assertEqual([], violations(mutated))


def early(step: str) -> str:
    """The workflow with the step text written before the login step of the build-docker job."""
    return insert_before_login(DockerTaskWorkflowCase.workflow, "build-docker", step + "\n")


class ReaderFixCase(unittest.TestCase):
    """Shapes the reader once missed or misjudged and now reads correctly."""

    def test_first_key_run_or_uses_push_is_caught(self) -> None:
        for step in (
            "      - run: docker push example/image:tag\n        name: Early push step\n",
            "      - uses: example/push@v1\n        with: {push: true}\n",
            "      - uses: example/push@v1\n        with:\n          push: true\n",
        ):
            with self.subTest(step=step):
                found = violations(early(step))
                self.assertIn("build-docker: no login before the first push step", found)

    def test_docker_global_options_before_push_are_caught(self) -> None:
        for command in (
            "docker --config /tmp/example push example/image:tag",
            "docker -H tcp://example:2375 image push example/image:tag",
            "docker --debug buildx build --push .",
        ):
            with self.subTest(command=command):
                step = f"      - name: Early push step\n        run: {command}\n"
                found = violations(early(step))
                self.assertIn("build-docker: no login before the first push step", found)

    def test_with_child_indented_deeper_than_ten_is_read(self) -> None:
        step = "      - name: Early push step\n        uses: example/push@v1\n        with:\n            push: true\n"
        found = violations(early(step))
        self.assertIn("build-docker: no login before the first push step", found)

    def test_push_false_is_not_a_push(self) -> None:
        for command in ("docker buildx build --push=false .", "docker buildx build --push=0 ."):
            with self.subTest(command=command):
                step = f"      - name: Local build step\n        run: {command}\n"
                self.assertEqual([], violations(early(step)))

    def test_env_value_after_run_is_not_part_of_the_script(self) -> None:
        step = (
            "      - name: Quiet step\n        run: true\n"
            "        env:\n          NOTE: docker push later\n"
        )
        self.assertEqual([], violations(early(step)))


class KnownLimitsCase(unittest.TestCase):
    """Each case asserts the reader's CURRENT behavior for a shape it does not judge correctly."""

    def test_limit_heredoc_body_push_is_flagged(self) -> None:
        step = "      - name: Doc step\n        run: |\n          cat <<EOF\n          docker push example/image\n          EOF\n"
        self.assertIn("build-docker: no login before the first push step", violations(early(step)))

    def test_limit_quoted_separator_before_push_words_is_flagged(self) -> None:
        step = '      - name: Echo step\n        run: echo "a; docker push example/image"\n'
        self.assertIn("build-docker: no login before the first push step", violations(early(step)))

    def test_limit_trailing_shell_comment_push_is_flagged(self) -> None:
        step = "      - name: Comment step\n        run: true # docker push example/image\n"
        self.assertIn("build-docker: no login before the first push step", violations(early(step)))

    def test_limit_registry_cache_source_is_flagged(self) -> None:
        step = "      - name: Cache step\n        run: docker buildx build --cache-from type=registry,ref=example/cache .\n"
        self.assertIn("build-docker: no login before the first push step", violations(early(step)))

    def test_limit_folded_run_push_is_missed(self) -> None:
        step = "      - name: Folded step\n        run: >\n          docker\n          push example/image\n"
        self.assertEqual([], violations(early(step)))

    def test_limit_plain_multiline_run_push_is_missed(self) -> None:
        step = "      - name: Plain step\n        run: docker\n          push example/image\n"
        self.assertEqual([], violations(early(step)))

    def test_limit_multiline_flow_with_push_is_missed(self) -> None:
        step = "      - name: Flow step\n        uses: example/push@v1\n        with: {\n          push: true}\n"
        self.assertEqual([], violations(early(step)))

    def test_limit_block_scalar_login_value_is_not_read(self) -> None:
        password = "          password: ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}"
        workflow = DockerTaskWorkflowCase.workflow
        mutated = workflow.replace(
            password, "          password: >-\n            ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}"
        )
        self.assertNotEqual(workflow, mutated)
        self.assertIn("build-docker: no login before the first push step", violations(mutated))

    def test_limit_inverted_credential_check_is_accepted(self) -> None:
        joined = '-z "$DOCKER_HUB_USERNAME" || -z "$DOCKER_HUB_ACCESS_TOKEN"'
        workflow = DockerTaskWorkflowCase.workflow
        mutated = workflow.replace(f"[[ {joined} ]]", f"! [[ {joined} ]]")
        if mutated == workflow:
            self.skipTest("the check is not written as a double-bracket test")
        self.assertEqual([], violations(mutated))


if __name__ == "__main__":
    unittest.main()
