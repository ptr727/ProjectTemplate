#!/usr/bin/env python3
"""Drive hub-fetch-run.py's unborn-HEAD rewrite, probe-failure path, and provenance handoff.

The git cases run in constructed temp repositories under a timeout. The network is stubbed, so
nothing here reaches GitHub.

Run as `python3 tests/test_hub_fetch_run.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
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
SNIPPET = ROOT / "catalog" / "snippets" / "hub-fetch-run.py"
TIMEOUT = 30
SHA = "0123456789abcdef0123456789abcdef01234567"


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


class ProvenanceTests(unittest.TestCase):
    SCRIPT = (
        b"import os, sys\n"
        b"open(sys.argv[1], 'w').write(os.environ.get('PROSE_GATE_PROVENANCE', '<unset>'))\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "seen.txt"
        self.urls: list[str] = []

    def fake_urlopen(self, sha_body: bytes | Exception):
        def opener(target, timeout=None):
            url = target if isinstance(target, str) else target.full_url
            self.urls.append(url)
            if url == hub.HUB_MAIN_SHA_URL:
                if isinstance(sha_body, Exception):
                    raise sha_body
                return FakeResponse(sha_body)
            return FakeResponse(self.SCRIPT)

        return opener

    def run_main(self, sha_body: bytes | Exception, env: dict[str, str] | None = None) -> str:
        scrubbed = {k: v for k, v in os.environ.items() if k != "PROSE_GATE_PROVENANCE"}
        scrubbed.update(env or {})
        with (
            mock.patch.dict(os.environ, scrubbed, clear=True),
            mock.patch.object(urllib.request, "urlopen", self.fake_urlopen(sha_body)),
        ):
            code = hub.main(["scripts/x.py", str(self.out)])
            self.assertEqual(code, 0)
        return self.out.read_text()

    def test_resolved_commit_names_the_fetched_copy(self) -> None:
        seen = self.run_main(SHA.encode())
        self.assertEqual(seen, f"ptr727/ProjectTemplate@{SHA}")
        self.assertTrue(self.urls[-1].endswith(f"/{SHA}/scripts/x.py"))

    def test_unresolved_commit_is_reported_as_such(self) -> None:
        seen = self.run_main(urllib.error.URLError("offline"))
        self.assertEqual(seen, "ptr727/ProjectTemplate@main (commit unresolved)")
        self.assertTrue(self.urls[-1].endswith("/main/scripts/x.py"))

    def test_malformed_commit_body_is_unresolved(self) -> None:
        seen = self.run_main(b"not a hash")
        self.assertIn("commit unresolved", seen)

    def test_callers_own_value_wins(self) -> None:
        seen = self.run_main(SHA.encode(), {"PROSE_GATE_PROVENANCE": "reproducing x@y"})
        self.assertEqual(seen, "reproducing x@y")

    def test_environment_is_restored(self) -> None:
        self.run_main(SHA.encode())
        self.assertNotIn("PROSE_GATE_PROVENANCE", os.environ)


if __name__ == "__main__":
    unittest.main()
