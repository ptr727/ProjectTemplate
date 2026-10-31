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
import os
import shutil
import subprocess
import tempfile
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
RESOLVED = f"ptr727/ProjectTemplate@{SHA} (hub-fetch-run main)"
UNRESOLVED = "ptr727/ProjectTemplate@main (hub-fetch-run, commit unresolved)"
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
        self.assertEqual(seen, f"ptr727/ProjectTemplate@{GIT_SHA} (hub-fetch-run main)")
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
            mock.patch.object(hub.subprocess, "run") as run,
            mock.patch.object(hub.shutil, "which", return_value="uv"),
            contextlib.redirect_stderr(self.stderr),
        ):
            self.assertEqual(1, hub.main([PROSE_PATH, str(self.out)]))
        run.assert_not_called()
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
        done: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess([], 3)
        with (
            self.environment(),
            mock.patch.object(urllib.request, "urlopen", self.opener(SHA_BODY, RECORD)),
            mock.patch.object(hub.subprocess, "run", return_value=done) as run,
            mock.patch.object(hub.shutil, "which", return_value="uv"),
            contextlib.redirect_stderr(self.stderr),
        ):
            self.assertEqual(3, hub.main([".github/actions/repo-gate/repo_gate.py", "--x"]))
        cmd = run.call_args.args[0]
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


if __name__ == "__main__":
    unittest.main()
