#!/usr/bin/env python3
"""Hold every tracked command-line entry point to CODESTYLE.md's argument rule by running it.

Each entry point runs with `--help`, with `-h`, and with an unknown `--bogus-flag`. Stubs ahead of
the real commands on PATH record any call to gh, git, or another network or host-changing command,
no credential is passed, and the proxy is a local listener that records every connection made to
it. Help exits 0 printing its usage. The unknown flag exits non-zero with a refusal that names it or
prints the usage, rather than a crash. No run reaches a stub or the listener, or changes the working
tree.

The set is discovered from the tree rather than listed, so a new script is held to the rule the
moment it is tracked. A hook its own host runs is not a command-line entry point and is listed in
NOT_COMMAND_LINE. A Windows script builds paths from APPDATA and LOCALAPPDATA as it loads, so the
sandbox sets both to keep a run off Windows from failing before the script's own argument handling.
A parser missing a required argument refuses on that before it reaches the unknown one, so a
refusal printing the usage passes as well as one naming the flag. The case is skipped on Windows,
where a stub without an executable extension shadows nothing and the real commands would run.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from host_capability import bash_or_skip

ROOT = Path(__file__).resolve().parents[1]

NOT_COMMAND_LINE = {
    "catalog/snippets/devcontainer/dotnet/post-create.sh": "a devcontainer lifecycle hook",
    "catalog/snippets/devcontainer/python/post-create.sh": "a devcontainer lifecycle hook",
    "host-setup/agent-safety/claude/gh-write-guard.py": "a Claude Code hook",
    "host-setup/agent-safety/claude/stray-process-sweep.py": "a Claude Code hook",
    "host-setup/agent-safety/claude/tool-containment.py": "a Claude Code hook",
    "host-setup/agent-safety/claude/test_install.py": "a test module",
}

RECORDED = ("git", "gh", "curl", "wget", "ssh", "sudo", "apt-get", "winget", "docker", "uv", "uvx")


def entry_points() -> list[str]:
    """Every tracked shell, PowerShell, or Python script run by name, tests and hooks aside."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "-z", "--", "*.sh", "*.ps1", "*.py"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    found = []
    for record in filter(None, listed.split("\0")):
        mode, path = record.split(" ", 1)[0], record.split("\t", 1)[1]
        if path.startswith(("tests/", ".husky/")) or path in NOT_COMMAND_LINE:
            continue
        if path.endswith(".py") and mode != "100755":
            text = (ROOT / path).read_text(encoding="utf-8")
            if 'if __name__ == "__main__":' not in text:
                continue
        found.append(path)
    return sorted(found)


def tree_state() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain=v1", "-uall"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


@contextmanager
def recording_proxy() -> Iterator[tuple[str, list[int]]]:
    """A proxy URL whose listener counts each connection and answers none, so every fetch fails."""
    connections: list[int] = []
    stop = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(0.1)

        def accept() -> None:
            while not stop.is_set():
                try:
                    peer, _ = server.accept()
                except TimeoutError:
                    continue
                connections.append(1)
                peer.close()

        worker = threading.Thread(target=accept, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.getsockname()[1]}", connections
        finally:
            stop.set()
            worker.join()


@unittest.skipIf(os.name == "nt", "a stub with no executable extension shadows nothing on Windows")
class EntryPointCase(unittest.TestCase):
    """Help answers with usage and exit 0, and `--bogus-flag` is refused, each touching nothing."""

    def setUp(self) -> None:
        self.before = tree_state()
        self.addCleanup(self.assert_tree_unchanged)

    def assert_tree_unchanged(self) -> None:
        self.assertEqual(tree_state(), self.before, "a help or refused run changed the tree")

    def command(self, path: str) -> list[str]:
        if path.endswith(".py"):
            return [sys.executable, str(ROOT / path)]
        if path.endswith(".sh"):
            return [bash_or_skip(), str(ROOT / path)]
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            raise unittest.SkipTest("no pwsh on PATH to run a PowerShell entry point")
        return [pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / path)]

    def run_entry(self, path: str, flag: str) -> tuple[subprocess.CompletedProcess[str], str]:
        command = self.command(path)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            stubs = scratch / "bin"
            stubs.mkdir()
            calls = scratch / "calls"
            for name in RECORDED:
                stub = stubs / name
                stub.write_text(
                    f'#!/bin/sh\necho "{name} $*" >>"{calls}"\nexit 1\n', encoding="utf-8"
                )
                stub.chmod(0o755)
            home = scratch / "home"
            home.mkdir()
            appdata = {name: str(scratch / name) for name in ("APPDATA", "LOCALAPPDATA")}
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in (
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                    "GH_ENTERPRISE_TOKEN",
                    "GITHUB_OUTPUT",
                    "GITHUB_ENV",
                    "GITHUB_PATH",
                    "GITHUB_STEP_SUMMARY",
                )
            }
            with recording_proxy() as (proxy, connections):
                env.update(
                    PATH=f"{stubs}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
                    HOME=str(home),
                    **appdata,
                    PYTHONDONTWRITEBYTECODE="1",
                    HTTP_PROXY=proxy,
                    HTTPS_PROXY=proxy,
                    http_proxy=proxy,
                    https_proxy=proxy,
                    NO_PROXY="",
                    no_proxy="",
                )
                result = subprocess.run(
                    [*command, flag],
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                    check=False,
                    env=env,
                    cwd=ROOT,
                )
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            recorded += "proxy connection\n" * len(connections)
        return result, recorded

    def test_help_prints_usage_and_exits_zero(self) -> None:
        for path in entry_points():
            for flag in ("--help", "-h"):
                with self.subTest(path=path, flag=flag):
                    result, calls = self.run_entry(path, flag)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage", result.stdout.lower(), result.stdout)
                    self.assertEqual(calls, "")

    def test_an_unknown_flag_is_refused(self) -> None:
        for path in entry_points():
            with self.subTest(path=path):
                result, calls = self.run_entry(path, "--bogus-flag")
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertTrue("bogus-flag" in output or "usage" in output.lower(), output)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(calls, "")

    def test_discovery_finds_each_kind(self) -> None:
        """The control: a discovery that matched nothing would pass both cases above vacuously."""
        found = entry_points()
        for expected in (
            "repo-config/configure.sh",
            "scripts/handoff.py",
            "scripts/prose_lint.py",
            "catalog/snippets/hub-fetch-run.py",
            "host-setup/bootstrap.ps1",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, found)
        for excluded in NOT_COMMAND_LINE:
            with self.subTest(excluded=excluded):
                self.assertTrue(
                    (ROOT / excluded).is_file(), "an exclusion names a file the tree no longer has"
                )


if __name__ == "__main__":
    unittest.main()
