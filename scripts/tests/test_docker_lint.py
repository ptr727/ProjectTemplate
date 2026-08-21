#!/usr/bin/env python3
"""Test bounded and observable Docker lint execution."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_lint


class FakeRunner:
    """Record commands and return configured Docker results."""

    def __init__(self, failure: int | None = None, timeout: bool = False) -> None:
        self.commands: list[tuple[list[str], int]] = []
        self.failure = failure
        self.timeout = timeout

    def __call__(
        self, command: list[str], timeout: int, *, capture_output: bool = False
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append((command, timeout))
        if self.timeout and command[:2] == ["docker", "run"]:
            raise docker_lint.CommandFailed(f"timed out after {timeout}s")
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "example@sha256:123\n", "")
        returncode = (
            self.failure if self.failure is not None and command[:2] == ["docker", "run"] else 0
        )
        return subprocess.CompletedProcess(command, returncode, "", "")


class DockerLintCase(unittest.TestCase):
    """Build a small tracked tree for each runner scenario."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def track(self, name: str, body: str = "content\n") -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "--", name], check=True)

    def invoke(self, selected: set[str], runner: FakeRunner) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = docker_lint.lint(self.root, 17, selected, runner)
        return result, output.getvalue()

    def test_quiet_success_reports_targets_phases_and_completion(self) -> None:
        self.track("README.md")
        runner = FakeRunner()
        result, output = self.invoke({"markdownlint"}, runner)
        self.assertEqual(0, result)
        self.assertIn("TARGETS markdownlint: 1 file(s)", output)
        self.assertIn("PHASE pull", output)
        self.assertIn("START inspect markdownlint (timeout 17s)", output)
        self.assertIn("COMPLETE inspect markdownlint", output)
        self.assertIn("PHASE execution: pulls complete, repository mounts begin", output)
        self.assertIn("START lint markdownlint (1 file(s)) (timeout 17s)", output)
        self.assertIn("COMPLETE lint markdownlint (1 file(s))", output)
        self.assertIn("RESULT success: 1 linter(s) completed", output)
        run = next(command for command, _ in runner.commands if command[:2] == ["docker", "run"])
        self.assertIn("--network=none", run)
        self.assertIn(f"type=bind,src={self.root},dst=/workdir,readonly", run)
        self.assertIn("example@sha256:123", run)
        self.assertTrue(all(timeout == 17 for _, timeout in runner.commands))

    def test_zero_targets_skips_pull_and_execution(self) -> None:
        runner = FakeRunner()
        result, output = self.invoke({"shellcheck"}, runner)
        self.assertEqual(0, result)
        self.assertIn("SKIP shellcheck: zero targets", output)
        self.assertIn("COMPLETE lint: zero applicable linters", output)
        self.assertEqual([], runner.commands)

    def test_timeout_is_distinct_from_container_failure(self) -> None:
        self.track("README.md")
        result, output = self.invoke({"markdownlint"}, FakeRunner(timeout=True))
        self.assertEqual(1, result)
        self.assertIn("TIMEOUT lint markdownlint", output)
        self.assertIn("RESULT failed: timed out after 17s", output)
        self.assertNotIn("FAILED lint markdownlint", output)

    def test_container_failure_reports_exit_code(self) -> None:
        self.track("README.md")
        result, output = self.invoke({"markdownlint"}, FakeRunner(failure=9))
        self.assertEqual(1, result)
        self.assertIn("FAILED lint markdownlint (1 file(s)) (exit 9)", output)
        self.assertIn("RESULT failed: container failure (exit 9)", output)

    def test_shellcheck_receives_each_tracked_file_as_one_argument(self) -> None:
        self.track("scripts/a shell.sh")
        runner = FakeRunner()
        result, _ = self.invoke({"shellcheck"}, runner)
        self.assertEqual(0, result)
        run = next(command for command, _ in runner.commands if command[:2] == ["docker", "run"])
        self.assertEqual("scripts/a shell.sh", run[-1])

    def test_timed_out_container_is_removed_with_a_shorter_bound(self) -> None:
        command = ["docker", "run", "--name", "bounded-lint", "image"]
        timed_out = subprocess.TimeoutExpired(command, 90)
        cleanup: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(["docker", "rm"], 0)
        with (
            mock.patch.object(subprocess, "run", side_effect=[timed_out, cleanup]) as run,
            self.assertRaisesRegex(docker_lint.CommandFailed, "timed out after 90s"),
        ):
            docker_lint.run_command(command, 90)
        self.assertEqual(90, run.call_args_list[0].kwargs["timeout"])
        self.assertEqual(["docker", "rm", "--force", "bounded-lint"], run.call_args_list[1].args[0])
        self.assertEqual(30, run.call_args_list[1].kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
