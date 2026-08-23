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
            raise docker_lint.CommandTimedOut(f"timed out after {timeout}s")
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
        self.assertIn(f'type=bind,"src={self.root}",dst=/workdir,readonly', run)
        self.assertIn("example@sha256:123", run)
        self.assertEqual("--", run[-2])
        self.assertEqual("README.md", run[-1])
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

    def test_process_start_failure_is_not_reported_as_timeout(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("docker")),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(docker_lint.CommandFailed, "could not start command"),
        ):
            docker_lint.run_step(
                "missing Docker", ["docker", "pull", "image"], 17, docker_lint.run_command
            )
        self.assertIn("FAILED missing Docker: could not start command:", output.getvalue())
        self.assertNotIn("TIMEOUT", output.getvalue())

    def test_shellcheck_receives_each_tracked_file_as_one_argument(self) -> None:
        self.track("scripts/a shell.sh")
        runner = FakeRunner()
        result, _ = self.invoke({"shellcheck"}, runner)
        self.assertEqual(0, result)
        run = next(command for command, _ in runner.commands if command[:2] == ["docker", "run"])
        self.assertEqual("scripts/a shell.sh", run[-1])

    def test_shellcheck_literal_marker_precedes_option_shaped_filename(self) -> None:
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "shellcheck")
        command = docker_lint.container_command(
            self.root, linter, "example@sha256:123", ["-release.sh"]
        )
        self.assertEqual(["--", "-release.sh"], command[-2:])

    def test_shfmt_literal_marker_precedes_option_shaped_filename(self) -> None:
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "shfmt")
        command = docker_lint.container_command(
            self.root, linter, "example@sha256:123", ["-release.sh"]
        )
        self.assertEqual(["-d", "--", "-release.sh"], command[-3:])

    def test_shellcheck_and_shfmt_pick_up_an_extensionless_shebang_script(self) -> None:
        self.track("ops/vps-backup-pull", "#!/usr/bin/env bash\nset -Eeuo pipefail\necho hi\n")
        for name in ("shellcheck", "shfmt"):
            linter = next(linter for linter in docker_lint.LINTERS if linter.name == name)
            self.assertEqual(["ops/vps-backup-pull"], docker_lint.tracked_files(self.root, linter))

    def test_extensionless_non_shell_file_is_not_picked_up(self) -> None:
        self.track("ops/README", "not a script\n")
        self.track("ops/run-me", "#!/usr/bin/env python3\nprint('hi')\n")
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "shellcheck")
        self.assertEqual([], docker_lint.tracked_files(self.root, linter))

    def test_extensionless_shebang_script_merges_with_glob_matched_scripts(self) -> None:
        self.track("regular.sh", "#!/usr/bin/env bash\necho hi\n")
        self.track("ops/vps-backup-pull", "#!/bin/sh\necho hi\n")
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "shellcheck")
        self.assertEqual(
            ["ops/vps-backup-pull", "regular.sh"],
            docker_lint.tracked_files(self.root, linter),
        )

    def test_extensionless_untracked_shebang_script_is_not_picked_up(self) -> None:
        path = self.root / "ops" / "vps-backup-pull"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "shellcheck")
        self.assertEqual([], docker_lint.tracked_files(self.root, linter))

    def test_shell_shebang_interpreter_rejects_bash_as_a_plain_argument(self) -> None:
        cases = {
            "#!/bin/bash": "bash",
            "#!/bin/sh": "sh",
            "#!/usr/bin/env bash": "bash",
            "#!/usr/bin/env\tbash": "bash",
            "#!/usr/bin/env -S bash -e": "bash",
            "#!/usr/bin/python bash": None,
            "#!/usr/bin/env python sh": None,
            "#!/usr/bin/env -S python -m sh": None,
            "#!/usr/bin/env": None,
            "not a shebang": None,
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(expected, docker_lint.shell_shebang_interpreter(line))

    def test_extensionless_script_naming_bash_only_as_an_argument_is_excluded(self) -> None:
        self.track("ops/run-me", "#!/usr/bin/python bash\nprint('hi')\n")
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "shellcheck")
        self.assertEqual([], docker_lint.tracked_files(self.root, linter))

    def test_extensionless_shebang_script_with_no_trailing_newline_is_picked_up(self) -> None:
        self.track("ops/vps-backup-pull", "#!/usr/bin/env bash")
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "shellcheck")
        self.assertEqual(["ops/vps-backup-pull"], docker_lint.tracked_files(self.root, linter))

    def test_shell_shebang_interpreter_walks_past_env_grammar(self) -> None:
        cases = {
            "#!/usr/bin/env FOO=1 bash": "bash",
            "#!/usr/bin/env -u bash python": None,
            "#!/usr/bin/env -i FOO=1 bash": "bash",
            "#!/usr/bin/env --unset=FOO bash": "bash",
            "#!/usr/bin/env -C /tmp bash": "bash",
            "#!/usr/bin/env FOO=1 BAR=2 sh": "sh",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(expected, docker_lint.shell_shebang_interpreter(line))

    def test_cspell_literal_marker_precedes_option_shaped_filename(self) -> None:
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "cspell")
        command = docker_lint.container_command(
            self.root, linter, "example@sha256:123", ["-release.md"]
        )
        self.assertEqual(["--", "-release.md"], command[-2:])

    def test_invalid_git_root_reports_failed_result(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = docker_lint.lint(root, 17, {"markdownlint"}, FakeRunner())
        self.assertEqual(1, result)
        self.assertIn("RESULT failed: target discovery failed:", output.getvalue())

    def test_mount_quotes_commas_and_embedded_quotes(self) -> None:
        root = Path('/tmp/docker-lint,"root')
        self.assertEqual(
            'type=bind,"src=/tmp/docker-lint,""root",dst=/workdir,readonly',
            docker_lint.docker_mount(root, "/workdir"),
        )

    def test_markdown_literal_marker_precedes_negated_filename(self) -> None:
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "markdownlint")
        command = docker_lint.container_command(
            self.root, linter, "example@sha256:123", ["!release.md"]
        )
        self.assertEqual(["--", "!release.md"], command[-2:])

    def test_long_file_lists_are_split_into_multiple_commands(self) -> None:
        linter = next(linter for linter in docker_lint.LINTERS if linter.name == "shellcheck")
        files = [f"scripts/{index}-{'x' * 200}.sh" for index in range(400)]
        batches = docker_lint.file_batches(linter, files)
        self.assertGreater(len(batches), 1)
        self.assertEqual(files, [file for batch in batches for file in batch])
        self.assertTrue(
            all(
                sum(len(file.encode()) + 4 for file in batch) <= docker_lint.MAX_FILE_ARGUMENT_BYTES
                for batch in batches
            )
        )

        runner = FakeRunner()
        with mock.patch.object(docker_lint, "tracked_files", return_value=files):
            result, output = self.invoke({"shellcheck"}, runner)
        self.assertEqual(0, result)
        runs = [command for command, _ in runner.commands if command[:2] == ["docker", "run"]]
        self.assertEqual(len(batches), len(runs))
        self.assertIn(f"batch 1/{len(batches)}", output)

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

    def test_cleanup_start_failure_remains_a_timeout_result(self) -> None:
        command = ["docker", "run", "--name", "bounded-lint", "image"]
        timed_out = subprocess.TimeoutExpired(command, 90)
        with (
            mock.patch.object(subprocess, "run", side_effect=[timed_out, OSError("no docker")]),
            self.assertRaisesRegex(
                docker_lint.CommandTimedOut, "cleanup could not start for bounded-lint"
            ),
        ):
            docker_lint.run_command(command, 90)


if __name__ == "__main__":
    unittest.main()
