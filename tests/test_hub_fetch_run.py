#!/usr/bin/env python3
"""Drive hub_fetch_run.py's unborn-HEAD rewrite, probe-failure path, provenance, and uv run.

The git cases run in constructed temp repositories, and the snippet bounds its own git probe with
a timeout. The network is stubbed, so nothing here reaches GitHub.

Run as `python3 tests/test_hub_fetch_run.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Self
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SNIPPET = ROOT / "catalog" / "snippets" / "hub_fetch_run.py"
TIMEOUT = 30
SHA = "0123456789abcdef0123456789abcdef01234567"
GIT_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def load_snippet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hub_fetch_run", SNIPPET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hub = load_snippet()


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=TIMEOUT,
    )


@contextlib.contextmanager
def in_dir(path: Path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class HeadProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def unborn_repo(self, name: str = "unborn") -> Path:
        repo = self.tmp / name
        repo.mkdir()
        git(repo, "init", "-q")
        return repo

    def born_repo(self) -> Path:
        repo = self.unborn_repo("born")
        git(repo, "commit", "-q", "--allow-empty", "-m", "first")
        return repo

    def test_unborn_head_rewrites_only_diff_head(self) -> None:
        argv = ["--check", "x", "--diff", "HEAD", ".", "--diff", "main", "HEAD"]
        with in_dir(self.unborn_repo()):
            result = hub.resolve_unborn_head(argv)
        self.assertEqual(
            result,
            ["--check", "x", "--diff", hub.EMPTY_TREE, ".", "--diff", "main", "HEAD"],
        )

    def test_unborn_head_leaves_other_diff_values_alone(self) -> None:
        argv = ["--diff", "origin/develop", ".", "HEAD"]
        with in_dir(self.unborn_repo()):
            self.assertEqual(hub.resolve_unborn_head(argv), argv)

    def test_born_head_rewrites_nothing(self) -> None:
        argv = ["--diff", "HEAD", "."]
        with in_dir(self.born_repo()):
            self.assertEqual(hub.resolve_unborn_head(argv), argv)

    def test_input_list_is_not_mutated(self) -> None:
        argv = ["--diff", "HEAD"]
        with in_dir(self.unborn_repo()):
            hub.resolve_unborn_head(argv)
        self.assertEqual(argv, ["--diff", "HEAD"])

    def test_head_is_unborn_distinguishes_states(self) -> None:
        with in_dir(self.unborn_repo()):
            self.assertTrue(hub.head_is_unborn())
        with in_dir(self.born_repo()):
            self.assertFalse(hub.head_is_unborn())

    def assert_probe_exits(self, run: mock.Mock) -> str:
        stderr = io.StringIO()
        with (
            mock.patch.object(hub.subprocess, "run", run),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            hub.resolve_unborn_head(["--diff", "HEAD"])
        self.assertEqual(caught.exception.code, 1)
        return stderr.getvalue()

    def test_non_git_directory_is_a_probe_failure(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        env = {"GIT_CEILING_DIRECTORIES": str(self.tmp)}
        stderr = io.StringIO()
        with (
            in_dir(plain),
            mock.patch.dict(os.environ, env),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            hub.resolve_unborn_head(["--diff", "HEAD"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("failed unexpectedly", stderr.getvalue())

    def test_nonzero_exit_with_stderr_is_a_probe_failure(self) -> None:
        probe = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"fatal: boom")
        message = self.assert_probe_exits(mock.Mock(return_value=probe))
        self.assertIn("boom", message)

    def test_exit_two_is_a_probe_failure(self) -> None:
        probe = subprocess.CompletedProcess([], 2, stdout=b"", stderr=b"")
        self.assert_probe_exits(mock.Mock(return_value=probe))

    def test_missing_git_executable_is_a_probe_failure(self) -> None:
        message = self.assert_probe_exits(mock.Mock(side_effect=FileNotFoundError("git")))
        self.assertIn("could not run git", message)

    def test_hung_probe_is_a_probe_failure(self) -> None:
        run = mock.Mock(side_effect=subprocess.TimeoutExpired("git", 30))
        message = self.assert_probe_exits(run)
        self.assertIn("could not run git", message)

    def test_probe_is_bounded_by_a_timeout(self) -> None:
        probe = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        run = mock.Mock(return_value=probe)
        with mock.patch.object(hub.subprocess, "run", run):
            hub.head_is_unborn()
        self.assertIsNotNone(run.call_args.kwargs.get("timeout"))

    def test_probe_is_not_run_without_a_diff_head_pair(self) -> None:
        run = mock.Mock(side_effect=AssertionError("probe must not run"))
        with mock.patch.object(hub.subprocess, "run", run):
            self.assertEqual(hub.resolve_unborn_head(["--diff", "main"]), ["--diff", "main"])
            self.assertEqual(hub.resolve_unborn_head(["HEAD", "--diff"]), ["HEAD", "--diff"])


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


SHA_BODY = SHA.encode()
PROSE_PATH = hub.PROSE_GATE_PATH
RESOLVED = f"ptr727/ProjectTemplate@{SHA} (hub_fetch_run.py main)"
UNRESOLVED = "ptr727/ProjectTemplate@main (hub_fetch_run.py, commit unresolved)"
RECORD = (
    b"import os, sys\n"
    b"with open(sys.argv[1], 'w') as out:\n"
    b"    out.write(os.environ.get('PROSE_GATE_PROVENANCE', '<unset>'))\n"
)
RAISE = b"raise RuntimeError('gate crashed')\n"
DATA_PATH = ".github/actions/prose-gate/prose-gate.json"
RATE_LIMITED = urllib.error.HTTPError("u", 403, "rate limit", {}, None)  # type: ignore[arg-type]
ENV_KEYS = ("PROSE_GATE_PROVENANCE", "GH_TOKEN", "GITHUB_TOKEN")


class ProvenanceTests(unittest.TestCase):
    # These stand uv in with a stub, so they run on the host lacking it, the one they are about.
    STUBBED = frozenset(
        {
            "test_the_script_runs_through_uv_with_no_project",
            "test_a_missing_uv_is_the_gate_not_running",
            "test_moving_main_between_fetches_refuses_a_mixed_pair",
        }
    )

    def setUp(self) -> None:
        if shutil.which("uv") is None and self._testMethodName not in self.STUBBED:
            self.skipTest("the fetched script runs through uv")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "seen.txt"
        self.requests: list[urllib.request.Request | str] = []
        self.timeouts: list[float | None] = []
        self.stderr = io.StringIO()
        self.git = mock.Mock(return_value=(None, "exit 128"))
        patcher = mock.patch.object(hub, "lookup_via_git", self.git)
        patcher.start()
        self.addCleanup(patcher.stop)

    def urls(self) -> list[str]:
        return [r if isinstance(r, str) else r.full_url for r in self.requests]

    def lookup_requests(self) -> list[urllib.request.Request]:
        return [
            r
            for r in self.requests
            if isinstance(r, urllib.request.Request) and r.full_url == hub.HUB_MAIN_SHA_URL
        ]

    def opener(self, lookup: bytes | Exception, script: bytes):
        def urlopen(target, timeout=None):
            self.requests.append(target)
            self.timeouts.append(timeout)
            url = target if isinstance(target, str) else target.full_url
            if url != hub.HUB_MAIN_SHA_URL:
                return FakeResponse(script)
            if isinstance(lookup, Exception):
                raise lookup
            return FakeResponse(lookup)

        return urlopen

    @contextlib.contextmanager
    def environment(self, env: dict[str, str] | None = None):
        """Patch a clean environment so an exported variable in the shell cannot leak in."""
        scrubbed = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        scrubbed.update(env or {})
        with mock.patch.dict(os.environ, scrubbed, clear=True):
            yield

    def run_main(
        self,
        lookup: bytes | Exception = SHA_BODY,
        env: dict[str, str] | None = None,
        path: str = PROSE_PATH,
        script: bytes = RECORD,
    ) -> str:
        """Run main() and return what the fetched script saw, checking the env right after."""
        before = None
        with (
            self.environment(env),
            mock.patch.object(urllib.request, "urlopen", self.opener(lookup, script)),
            contextlib.redirect_stderr(self.stderr),
        ):
            before = os.environ.get("PROSE_GATE_PROVENANCE")
            self.assertEqual(hub.main([path, str(self.out)]), 0)
            self.assertEqual(os.environ.get("PROSE_GATE_PROVENANCE"), before)
        return self.out.read_text()

    def test_resolved_commit_names_the_fetched_copy(self) -> None:
        self.assertEqual(self.run_main(), RESOLVED)
        self.assertTrue(self.urls()[-1].endswith(f"/{SHA}/{PROSE_PATH}"))

    def test_value_does_not_look_like_a_ci_pin(self) -> None:
        self.assertNotRegex(self.run_main(), r"^[^ ]+@[^ ]+$")

    def test_lookup_only_for_the_prose_gate(self) -> None:
        seen = self.run_main(path=".github/actions/repo-gate/repo_gate.py")
        self.assertEqual(seen, "<unset>")
        self.assertEqual(self.lookup_requests(), [])
        self.git.assert_not_called()
        self.assertTrue(self.urls()[-1].endswith("/main/.github/actions/repo-gate/repo_gate.py"))

    def test_fallback_causes_print_a_line_and_the_gate_still_runs(self) -> None:
        cases: dict[str, bytes | Exception] = {
            "HTTP 403": urllib.error.HTTPError("u", 403, "rate limit", {}, None),  # type: ignore[arg-type]
            "HTTP 429": urllib.error.HTTPError("u", 429, "slow down", {}, None),  # type: ignore[arg-type]
            "URLError": urllib.error.URLError("offline"),
            "IncompleteRead": http.client.IncompleteRead(b"ab"),
            "UnicodeDecodeError": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
            "unexpected response body": b"\xff not a hash",
        }
        pairs = list(cases.items())
        for bad in (SHA[:8], SHA + SHA[:24], SHA + "a", SHA.upper(), f"{SHA}\n{SHA}"):
            pairs.append(("unexpected response body", bad.encode()))
        for cause, lookup in pairs:
            with self.subTest(cause=cause):
                self.stderr.seek(0)
                self.stderr.truncate()
                self.requests.clear()
                self.assertEqual(self.run_main(lookup), UNRESOLVED)
                line = self.stderr.getvalue()
                self.assertIn(cause, line)
                self.assertIn("git ls-remote exit 128", line)
                self.assertEqual(line.count("\n"), 1, line)
                self.assertTrue(self.urls()[-2].endswith(f"/main/{DATA_PATH}"), self.urls())
                self.assertTrue(self.urls()[-1].endswith(f"/main/{PROSE_PATH}"), self.urls())
                self.assertEqual(len(self.urls()), 5, self.urls())

    def test_git_fallback_pins_the_script_and_its_data_to_one_commit(self) -> None:
        """A rate-limited API lookup still fetches both files at one commit, so they pair."""
        self.git.return_value = (GIT_SHA, "")
        seen = self.run_main(RATE_LIMITED)
        self.assertEqual(seen, f"ptr727/ProjectTemplate@{GIT_SHA} (hub_fetch_run.py main)")
        fetched = [u for u in self.urls() if u != hub.HUB_MAIN_SHA_URL]
        self.assertEqual(
            fetched,
            [
                f"{hub.HUB_RAW_BASE}/{GIT_SHA}/{DATA_PATH}",
                f"{hub.HUB_RAW_BASE}/{GIT_SHA}/{PROSE_PATH}",
            ],
        )
        self.assertEqual(self.stderr.getvalue(), "")

    def test_moving_main_between_fetches_refuses_a_mixed_pair(self) -> None:
        """Without a commit, a push landing mid-fetch changes a file between the two reads."""
        counts: dict[str, int] = {}

        def urlopen(target, timeout=None):
            url = target if isinstance(target, str) else target.full_url
            if url == hub.HUB_MAIN_SHA_URL:
                raise RATE_LIMITED
            counts[url] = counts.get(url, 0) + 1
            return FakeResponse(RECORD + b"#" * (counts[url] if url.endswith(".json") else 0))

        with (
            self.environment(),
            mock.patch.object(urllib.request, "urlopen", urlopen),
            mock.patch.object(hub.subprocess, "Popen") as popen,
            mock.patch.object(hub.shutil, "which", return_value="uv"),
            contextlib.redirect_stderr(self.stderr),
        ):
            self.assertEqual(1, hub.main([PROSE_PATH, str(self.out)]))
        popen.assert_not_called()
        self.assertIn("mixed pair was refused", self.stderr.getvalue())

    def test_a_pinned_commit_is_fetched_once(self) -> None:
        self.run_main()
        fetched = [u for u in self.urls() if u != hub.HUB_MAIN_SHA_URL]
        self.assertEqual(len(fetched), 2)

    def test_git_fallback_is_not_run_when_the_api_resolves(self) -> None:
        self.assertEqual(self.run_main(), RESOLVED)
        self.git.assert_not_called()

    def test_token_header_sent_only_when_set(self) -> None:
        self.run_main()
        self.assertIsNone(self.lookup_requests()[-1].get_header("Authorization"))
        self.assertEqual(
            self.lookup_requests()[-1].get_header("Accept"), "application/vnd.github.sha"
        )
        self.run_main(env={"GH_TOKEN": "tok-one"})
        self.assertEqual(self.lookup_requests()[-1].get_header("Authorization"), "Bearer tok-one")
        self.run_main(env={"GITHUB_TOKEN": "tok-two"})
        self.assertEqual(self.lookup_requests()[-1].get_header("Authorization"), "Bearer tok-two")

    def test_token_is_never_printed(self) -> None:
        self.run_main(urllib.error.URLError("offline"), env={"GH_TOKEN": "tok-secret"})
        self.assertNotIn("tok-secret", self.stderr.getvalue())

    def test_caller_value_survives(self) -> None:
        seen = self.run_main(env={"PROSE_GATE_PROVENANCE": "reproducing x@y"})
        self.assertEqual(seen, "reproducing x@y")

    def test_whitespace_only_caller_value_is_overwritten(self) -> None:
        self.assertEqual(self.run_main(env={"PROSE_GATE_PROVENANCE": "  "}), RESOLVED)

    def test_a_failing_script_passes_its_exit_code_through(self) -> None:
        """The script runs in its own process, so this one's environment is never touched."""
        for caller, expected in (("  ", "  "), (None, None)):
            with self.subTest(caller=caller):
                env = {} if caller is None else {"PROSE_GATE_PROVENANCE": caller}
                with (
                    self.environment(env),
                    mock.patch.object(urllib.request, "urlopen", self.opener(SHA_BODY, RAISE)),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(1, hub.main([PROSE_PATH]))
                    self.assertEqual(os.environ.get("PROSE_GATE_PROVENANCE"), expected)

    def test_the_gate_data_file_is_fetched_from_the_same_commit(self) -> None:
        """The gate reads its data file beside itself, so a single-file fetch cannot run it."""
        self.run_main()
        data = ".github/actions/prose-gate/prose-gate.json"
        self.assertTrue(self.urls()[-2].endswith(f"/{SHA}/{data}"), self.urls())

    def test_the_script_runs_through_uv_with_no_project(self) -> None:
        """A project sync would write a lock file and an environment into the caller's tree."""
        child = mock.Mock()
        child.wait.return_value = 3
        with (
            self.environment(),
            mock.patch.object(urllib.request, "urlopen", self.opener(SHA_BODY, RECORD)),
            mock.patch.object(hub.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(hub.shutil, "which", return_value="uv"),
            contextlib.redirect_stderr(self.stderr),
        ):
            self.assertEqual(3, hub.main([".github/actions/repo-gate/repo_gate.py", "--x"]))
        cmd = popen.call_args.args[0]
        self.assertEqual(["run", "--no-project", "--script"], cmd[1:4])
        self.assertEqual(["--x"], cmd[5:])

    def test_a_missing_uv_is_the_gate_not_running(self) -> None:
        with (
            self.environment(),
            mock.patch.object(urllib.request, "urlopen", self.opener(SHA_BODY, RECORD)),
            mock.patch.object(hub.shutil, "which", return_value=None),
            contextlib.redirect_stderr(self.stderr),
        ):
            self.assertEqual(1, hub.main([PROSE_PATH]))
        self.assertIn("uv is not on PATH, so the gate did not run", self.stderr.getvalue())

    def test_lookup_and_fetch_use_separate_timeouts(self) -> None:
        self.run_main()
        self.assertEqual(self.timeouts[0], hub.LOOKUP_TIMEOUT)
        self.assertLess(hub.LOOKUP_TIMEOUT, self.timeouts[1])

    def test_token_header_is_unredirected(self) -> None:
        self.run_main(env={"GH_TOKEN": "tok-one"})
        request = self.lookup_requests()[-1]
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.unredirected_hdrs.get("Authorization"), "Bearer tok-one")


# Runs the snippet as `__main__` with the network stubbed, so its own re-raise is what ends it.
INTERRUPT_LAUNCHER = """
import runpy, sys, urllib.request

class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        return self.body

def urlopen(target, timeout=None):
    url = target if isinstance(target, str) else target.full_url
    return Response(SHA if url.endswith("/commits/main") else b"print('gate')\\n")

SHA = sys.argv[2].encode()
urllib.request.urlopen = urlopen
sys.argv = [sys.argv[1], *sys.argv[3:]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@unittest.skipIf(sys.platform == "win32", "Windows cannot deliver either signal for a script")
class InterruptTests(unittest.TestCase):
    """A signal mid-run stops the script, removes the fetched tree, and ends the helper by it.

    The script is a stub uv that marks itself started and then sleeps, so the signal lands while
    the helper holds both. The helper runs under its interpreter directly, since `uv run` would
    exit with a status rather than die by the signal.
    """

    def interrupt(self, sig: signal.Signals, group: bool) -> tuple[int, str, list[str]]:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        ready, stubs, scratch = outside / "started", outside / "bin", outside / "tmp"
        stubs.mkdir()
        scratch.mkdir()
        uv = stubs / "uv"
        uv.write_text(f'#!/bin/sh\n: >"{ready}"\nexec sleep {TIMEOUT}\n', encoding="utf-8")
        uv.chmod(0o755)
        launcher = outside / "launch.py"
        launcher.write_text(INTERRUPT_LAUNCHER, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        env.update(TMPDIR=str(scratch), PATH=f"{stubs}{os.pathsep}{env.get('PATH', '')}")
        cmd = [sys.executable, str(launcher), str(SNIPPET), SHA, PROSE_PATH, "."]
        helper = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + TIMEOUT
            while not ready.exists():
                if helper.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("the stub never started: " + helper.communicate()[1])
                time.sleep(0.05)
            self.assertNotEqual([], list(scratch.iterdir()), "the helper holds no fetched tree")
            (os.killpg if group else os.kill)(helper.pid, sig)
            _, stderr = helper.communicate(timeout=TIMEOUT)
            self.assert_group_ended(helper.pid)
        finally:
            if helper.poll() is None:
                os.killpg(helper.pid, signal.SIGKILL)
                helper.communicate(timeout=TIMEOUT)
        return helper.returncode, stderr, [p.name for p in scratch.iterdir()]

    def assert_group_ended(self, pgid: int) -> None:
        """Every process the helper started is gone, which a survivor would fail by holding the group."""
        deadline = time.monotonic() + TIMEOUT
        while True:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() > deadline:
                os.killpg(pgid, signal.SIGKILL)
                raise AssertionError("a process the helper started outlived it")
            time.sleep(0.05)

    def test_a_signal_mid_run_stops_the_script_and_removes_the_tree(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            for group in (False, True):
                with self.subTest(signal=sig.name, group=group):
                    status, stderr, left = self.interrupt(sig, group)
                    self.assertEqual(-sig, status, stderr)
                    self.assertIn(f"hub_fetch_run: interrupted by {sig.name}.", stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual([], left)


class GitLookupTests(unittest.TestCase):
    """lookup_via_git reads one `git ls-remote` line, and every other shape is a named cause."""

    def lookup(self, run: mock.Mock) -> tuple[str | None, str]:
        with mock.patch.object(hub.subprocess, "run", run):
            return hub.lookup_via_git()

    def test_one_main_line_is_its_commit(self) -> None:
        done = subprocess.CompletedProcess([], 0, f"{GIT_SHA}\trefs/heads/main\n", "")
        run = mock.Mock(return_value=done)
        self.assertEqual(self.lookup(run), (GIT_SHA, ""))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["git", "ls-remote", hub.HUB_GIT_URL, "refs/heads/main"])
        self.assertEqual(kwargs["timeout"], hub.LOOKUP_TIMEOUT)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)

    def test_every_other_shape_names_its_cause(self) -> None:
        line = f"{GIT_SHA}\trefs/heads/main\n"
        cases: list[tuple[str, mock.Mock]] = [
            ("exit 128", mock.Mock(return_value=subprocess.CompletedProcess([], 128, "", "x"))),
            ("FileNotFoundError", mock.Mock(side_effect=FileNotFoundError("git"))),
            ("timed out", mock.Mock(side_effect=subprocess.TimeoutExpired("git", 5))),
        ]
        for out in (
            "",
            GIT_SHA[:8] + "\trefs/heads/main\n",
            line + line,
            GIT_SHA.upper() + line[40:],
        ):
            done = subprocess.CompletedProcess([], 0, out, "")
            cases.append(("unexpected output", mock.Mock(return_value=done)))
        for cause, run in cases:
            with self.subTest(cause=cause, run=run):
                self.assertEqual(self.lookup(run), (None, cause))


DOCKER_LINT = "scripts/docker_lint.py"
LINT_TASKS = {
    "Lint: EditorConfig": "editorconfig-checker",
    "Lint: Workflows": "actionlint",
    "Lint: Markdown": "markdownlint",
    "Lint: Spelling": "cspell",
}
HELPER = "${workspaceFolder}/hub_fetch_run.py"
TASK_SNIPPETS = ("vscode-tasks.json", "vscode-tasks-python.json")
HUSKY_HOOK = ROOT / "catalog" / "snippets" / "husky" / "pre-commit"


def snippet_tasks(name: str) -> dict[str, dict]:
    """The named snippet's tasks by label. Its only JSONC is a comment on a line of its own."""
    text = (ROOT / "catalog" / "snippets" / "configs" / name).read_text(encoding="utf-8")
    plain = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    return {task["label"]: task for task in json.loads(plain)["tasks"]}


class LintTaskTests(unittest.TestCase):
    def forms(self, task: dict) -> list[list[str]]:
        """Each command line a task can run, its Windows override included where it has one."""
        forms = [[task["command"], *task["args"]]]
        if "windows" in task:
            forms.append([task["windows"]["command"], *task["windows"]["args"]])
        return forms

    def test_each_docker_lint_task_runs_the_wrapper_through_the_helper(self) -> None:
        for name in TASK_SNIPPETS:
            tasks = snippet_tasks(name)
            for label, linter in LINT_TASKS.items():
                for form in self.forms(tasks[label]):
                    with self.subTest(snippet=name, task=label, command=form[0]):
                        self.assertIn(HELPER, form)
                        self.assertEqual(form[-3:], [DOCKER_LINT, "--linter", linter])

    def test_every_helper_reference_names_the_root_copy(self) -> None:
        """A task naming the helper anywhere but the root fails in a repo that keeps it there."""
        for name in TASK_SNIPPETS:
            for label, task in snippet_tasks(name).items():
                for form in self.forms(task) if "command" in task else []:
                    for arg in form:
                        if arg.endswith("hub_fetch_run.py"):
                            with self.subTest(snippet=name, task=label, arg=arg):
                                self.assertEqual(arg, HELPER)

    def test_husky_hook_calls_the_root_copy_after_moving_to_the_top_level(self) -> None:
        """The hook resolves the helper from the repo root, so the move to the top level comes first."""
        lines = HUSKY_HOOK.read_text(encoding="utf-8").splitlines()
        cd = lines.index('cd "$(git rev-parse --show-toplevel)" || exit 1')
        helper = lines.index('hub_fetch_run="$PWD/hub_fetch_run.py"')
        self.assertLess(cd, helper)

    def test_ruff_tasks_take_the_tool_s_own_casing(self) -> None:
        """A carrying repository copies these labels, so a capitalized one is a finding there."""
        hub_tasks = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))[
            "tasks"
        ]
        sources = {
            "vscode-tasks-python.json": list(snippet_tasks("vscode-tasks-python.json").values()),
            ".vscode/tasks.json": hub_tasks,
        }
        for source, tasks in sources.items():
            names = [task["label"] for task in tasks]
            names += [name for task in tasks for name in task.get("dependsOn", [])]
            with self.subTest(source=source):
                self.assertLessEqual({"ruff format", "ruff check"}, set(names))
                self.assertEqual(
                    [
                        name
                        for name in names
                        if name.lower().startswith("ruff") and not name.startswith("ruff")
                    ],
                    [],
                )

    def test_no_task_binds_the_live_tree_into_a_container(self) -> None:
        """The wrapper's snapshot is the only mount, so no task may call Docker itself."""
        for name in TASK_SNIPPETS:
            for label, task in snippet_tasks(name).items():
                if "command" in task:
                    with self.subTest(snippet=name, task=label):
                        self.assertNotEqual(task["command"], "docker")


class FetchedWrapperTests(unittest.TestCase):
    """Run the hub's real wrapper from where the helper fetches it, which is outside the tree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.urls: list[str] = []
        self.real_popen = subprocess.Popen
        self.stdout = ""
        self.stderr = io.StringIO()
        self.commit: str | None = SHA

    def urlopen(self, target: str, timeout: float | None = None) -> FakeResponse:
        self.urls.append(target)
        _, _, path = target.partition(f"{hub.HUB_RAW_BASE}/{self.commit or 'main'}/")
        return FakeResponse((ROOT / path).read_bytes())

    def uv_as_python(self, cmd: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
        """Stand in for `uv run --no-project --script`, with nothing from this tree importable.

        The real Popen is held from before the patch, since the patch replaces it module-wide.
        """
        clean = {k: v for k, v in env.items() if k != "PYTHONPATH"}
        child = self.real_popen(
            [sys.executable, *cmd[4:]],
            env=clean,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.tmp,
        )
        out, err = child.communicate(timeout=TIMEOUT)
        self.stdout = out + err
        return child

    def run_wrapper(self) -> int:
        with (
            mock.patch.object(urllib.request, "urlopen", self.urlopen),
            mock.patch.object(hub.shutil, "which", return_value="uv"),
            mock.patch.object(hub.subprocess, "Popen", self.uv_as_python),
            mock.patch.object(hub, "resolve_main_commit", return_value=self.commit),
            contextlib.redirect_stderr(self.stderr),
        ):
            return hub.main([DOCKER_LINT, "--help"])

    def test_the_wrapper_runs_with_no_other_hub_file_beside_it(self) -> None:
        self.assertEqual(self.run_wrapper(), 0, self.stdout)

    def test_the_wrapper_offers_every_linter_a_task_names(self) -> None:
        self.assertEqual(self.run_wrapper(), 0, self.stdout)
        choices = re.search(r"--linter \{([^}]+)\}", self.stdout)
        assert choices is not None, self.stdout
        self.assertLessEqual(set(LINT_TASKS.values()), set(choices.group(1).split(",")))

    def fetched(self, ref: str) -> list[str]:
        return [f"{hub.HUB_RAW_BASE}/{ref}/{DOCKER_LINT}"]

    def test_the_wrapper_alone_comes_from_the_resolved_commit(self) -> None:
        self.assertEqual(self.run_wrapper(), 0, self.stdout)
        self.assertEqual(self.urls, self.fetched(SHA))

    def test_the_run_names_the_hub_commit_it_ran_from(self) -> None:
        """A lint result names its hub commit, so two runs that disagree can be attributed."""
        self.assertEqual(self.run_wrapper(), 0, self.stdout)
        self.assertIn(f"ptr727/ProjectTemplate@{SHA}", self.stderr.getvalue())

    def test_an_unresolved_commit_reads_main_once_and_says_so(self) -> None:
        """A lone file cannot form a mixed set, so no double read guards it."""
        self.commit = None
        self.assertEqual(self.run_wrapper(), 0, self.stdout)
        self.assertEqual(self.urls, self.fetched("main"))
        self.assertIn("ptr727/ProjectTemplate@main", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
