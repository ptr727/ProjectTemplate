#!/usr/bin/env python3
"""Hold every tracked command-line entry point to CODESTYLE.md's argument rule by running it.

Each entry point runs with `--help`, with `-h`, and with an unknown `--bogus-flag`. Stubs ahead of
the real commands on PATH record any call to gh, git, or another network or host-changing command,
no credential is passed, every home and config-home variable points into a scratch directory, and
the proxy is a local listener that records every connection made to it. Help exits 0 and prints the
same text for both spellings, opening on a usage line that names the script, so a default mode
whose report merely mentions usage does not pass. The unknown flag exits non-zero without a crash,
with a refusal naming it as an unknown option. A script needing a subcommand or a positional
refuses on the missing argument before it reaches the flag, so BEFORE_FLAG gives it arguments that
satisfy its parser, and the run with them is the one that must name the flag. No run reaches a stub
or the listener, leaves a process running, or changes the working tree, ignored paths included.

The set is discovered from the tree rather than listed, so a new script is held to the rule the
moment it is tracked, an extensionless file opening on a shebang included. A hook its own host runs
is not a command-line entry point and is listed in NOT_COMMAND_LINE. A Windows script builds paths
from APPDATA and LOCALAPPDATA as it loads, so the sandbox sets both to keep a run off Windows from
failing before the script's own argument handling.

A process the run leaves behind is found by a token in the environment it inherited, read from
/proc, so that check runs only on a host that has /proc. On Windows only the PowerShell entry points
run. PowerShell's own command lookup finds a `.cmd` stub first on PATH, while a process Python
starts there is found by its `.exe` name, which a stub does not shadow. So a PowerShell wrapper that
hands its arguments to Python is held there only as far as PowerShell's own calls.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from host_capability import bash_or_skip

ROOT = Path(__file__).resolve().parents[1]

NOT_COMMAND_LINE = {
    "catalog/snippets/devcontainer/dotnet/post-create.sh": "a devcontainer lifecycle hook",
    "catalog/snippets/devcontainer/python/post-create.sh": "a devcontainer lifecycle hook",
    "catalog/snippets/husky/pre-commit": "a Git hook",
    "host-setup/agent-safety/claude/gh-write-guard.py": "a Claude Code hook",
    "host-setup/agent-safety/claude/stray-process-sweep.py": "a Claude Code hook",
    "host-setup/agent-safety/claude/tool-containment.py": "a Claude Code hook",
    "host-setup/agent-safety/claude/test_install.py": "a test module",
}

BEFORE_FLAG = {
    "repo-config/configure.sh": ("check",),
    "scripts/canonical_review.py": ("list",),
    "scripts/carry.py": ("check", "Example", "--target", "Example-target"),
    "scripts/handoff.py": ("current", "--repo", "Example/Example"),
    "scripts/local_review.py": ("status",),
    "scripts/pr_review.py": ("--repo", "Example/Example", "status", "1"),
    "spec/resolve_description.py": ("Example-registry.json", "Example"),
}

REFUSED_AS_OPTION = re.compile(
    r"(unrecogni[sz]ed argument|unknown (option|argument|flag|parameter)"
    r"|cannot be found that matches parameter name)[^\n]*bogus-flag",
    re.IGNORECASE,
)

RECORDED = ("git", "gh", "curl", "wget", "ssh", "sudo", "apt-get", "winget", "docker", "uv", "uvx")

HOMES = (
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "GH_CONFIG_DIR",
    "CLAUDE_HOME",
    "CLAUDE_CONFIG_DIR",
    "AGENTS_HOME",
    "CODEX_HOME",
)

WITHHELD = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_OUTPUT",
    "GITHUB_ENV",
    "GITHUB_PATH",
    "GITHUB_STEP_SUMMARY",
)

RUN_TOKEN = "CLI_USAGE_RUN"


def has_shebang(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(2) == b"#!"


def entry_points(root: Path = ROOT) -> list[str]:
    """Every tracked script run by name, tests and hooks aside.

    A script is a `.sh` or `.ps1` file, a `.py` file that is executable or guards a main, or a file
    with no extension that opens on a shebang.
    """
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "-z"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    found = []
    for record in filter(None, listed.split("\0")):
        mode, path = record.split(" ", 1)[0], record.split("\t", 1)[1]
        if mode not in ("100644", "100755"):
            continue
        if path.startswith(("tests/", ".husky/")) or path in NOT_COMMAND_LINE:
            continue
        suffix = PurePosixPath(path).suffix
        if suffix == ".py" and mode != "100755":
            text = (root / path).read_text(encoding="utf-8")
            if 'if __name__ == "__main__":' not in text:
                continue
        elif suffix not in (".py", ".sh", ".ps1") and (suffix or not has_shebang(root / path)):
            continue
        found.append(path)
    return sorted(found)


def interpreter(root: Path, path: str) -> str | None:
    """The runtime an entry point needs, from its extension or else its shebang, or None if neither names one."""
    by_suffix = {".py": "python", ".sh": "bash", ".ps1": "pwsh"}
    suffix = PurePosixPath(path).suffix
    if suffix:
        return by_suffix.get(suffix)
    with (root / path).open(encoding="utf-8", errors="replace") as handle:
        words = handle.readline()[2:].split()
    name = PurePosixPath(words[0]).name if words else ""
    if name == "env" and len(words) > 1:
        name = words[1]
    if name.startswith("python"):
        return "python"
    if name in ("bash", "sh"):
        return "bash"
    return "pwsh" if name == "pwsh" else None


def tree_state() -> dict[str, tuple[str, int, int]]:
    """Every changed, untracked, or ignored path, with its size and modification time.

    The size and time catch a second write to a path that was already listed, which its status line
    alone would not show.
    """
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain=v1", "-z", "-uall", "--ignored"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=True,
    ).stdout
    records = iter(listed.split("\0"))
    state = {}
    for record in records:
        if not record:
            continue
        status, path = record[:2], record[3:]
        if "R" in status or "C" in status:
            next(records, None)
        try:
            stat = os.lstat(ROOT / path)
        except FileNotFoundError:
            state[path] = (status, -1, -1)
        else:
            state[path] = (status, stat.st_size, stat.st_mtime_ns)
    return state


def tree_changes(before: dict[str, tuple[str, int, int]]) -> str:
    after = tree_state()
    changed = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    return "".join(f"changed {path}\n" for path in changed)


def tagged_processes(token: str) -> list[int]:
    """Every live process whose inherited environment carries this run's token, where /proc exists."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    needle = f"{RUN_TOKEN}={token}".encode()
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.append(int(entry.name))
    return found


def stop_survivors(token: str) -> str:
    """Kill every process the run left behind and name each one, waiting a bounded time for them to go."""
    survivors = tagged_processes(token)
    named = []
    for pid in survivors:
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            continue
        named.append(f"left running: {command.strip()}\n")
    deadline = time.monotonic() + 10
    while survivors and tagged_processes(token) and time.monotonic() < deadline:
        time.sleep(0.05)
    return "".join(named)


def write_stubs(stubs: Path, calls: Path) -> None:
    stubs.mkdir()
    for name in RECORDED:
        if os.name == "nt":
            stub = stubs / f"{name}.cmd"
            stub.write_bytes(f'@>>"{calls}" echo {name} %*\r\n@exit /b 1\r\n'.encode())
        else:
            stub = stubs / name
            stub.write_text(f'#!/bin/sh\necho "{name} $*" >>"{calls}"\nexit 1\n', encoding="utf-8")
            stub.chmod(0o755)


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


class EntryPointCase(unittest.TestCase):
    """Help answers with usage and exit 0, and `--bogus-flag` is refused, each touching nothing."""

    def command(self, path: str) -> list[str]:
        kind = interpreter(ROOT, path)
        if kind is None:
            self.fail(f"{path} names no runtime this case knows, by extension or by shebang")
        if os.name == "nt" and kind != "pwsh":
            raise unittest.SkipTest(
                "a stub shadows a command only for PowerShell's lookup on Windows"
            )
        if kind == "python":
            return [sys.executable, str(ROOT / path)]
        if kind == "bash":
            return [bash_or_skip(), str(ROOT / path)]
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            raise unittest.SkipTest("no pwsh on PATH to run a PowerShell entry point")
        return [pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / path)]

    def run_entry(self, path: str, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
        """The run's result and its effects, one line per stub call, connection, survivor, or changed path."""
        command = self.command(path)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            calls = scratch / "calls"
            write_stubs(scratch / "bin", calls)
            homes = {name: scratch / name for name in HOMES}
            for home in homes.values():
                home.mkdir()
            token = secrets.token_hex(16)
            env = {key: value for key, value in os.environ.items() if key not in WITHHELD}
            env.update({name: str(home) for name, home in homes.items()})
            env.update(
                PATH=f"{scratch / 'bin'}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
                GIT_CONFIG_GLOBAL=str(scratch / "gitconfig"),
                PYTHONDONTWRITEBYTECODE="1",
                NO_PROXY="",
                no_proxy="",
            )
            env[RUN_TOKEN] = token
            before = tree_state()
            with recording_proxy() as (proxy, connections):
                env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy)
                try:
                    result = subprocess.run(
                        [*command, *args],
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
                finally:
                    survivors = stop_survivors(token)
            effects = calls.read_text(encoding="utf-8") if calls.exists() else ""
            effects += "proxy connection\n" * len(connections) + survivors
        return result, effects + tree_changes(before)

    def test_help_prints_usage_and_exits_zero(self) -> None:
        for path in entry_points():
            outputs = {}
            for flag in ("--help", "-h"):
                with self.subTest(path=path, flag=flag):
                    result, effects = self.run_entry(path, flag)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    first = (result.stdout.strip().splitlines() or [""])[0].lower()
                    self.assertTrue(first.startswith("usage:"), result.stdout)
                    self.assertIn(PurePosixPath(path).stem.lower(), first)
                    self.assertEqual(effects, "")
                    outputs[flag] = result.stdout
            if len(outputs) == 2:
                with self.subTest(path=path, flag="both"):
                    self.assertEqual(outputs["-h"], outputs["--help"])

    def test_an_unknown_flag_is_refused(self) -> None:
        for path in entry_points():
            before_flag = BEFORE_FLAG.get(path, ())
            for before in dict.fromkeys(((), before_flag)):
                with self.subTest(path=path, before=" ".join(before)):
                    result, effects = self.run_entry(path, *before, "--bogus-flag")
                    output = result.stdout + result.stderr
                    self.assertNotEqual(result.returncode, 0, output)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(effects, "")
                    if before == before_flag:
                        self.assertRegex(output, REFUSED_AS_OPTION)

    def test_discovery_finds_each_kind(self) -> None:
        """The control: a discovery that matched nothing would pass both cases above vacuously."""
        found = entry_points()
        for expected in (
            "repo-config/configure.sh",
            "scripts/handoff.py",
            "scripts/prose_lint.py",
            "catalog/snippets/hub-fetch-run.py",
            "host-setup/bootstrap.ps1",
            *BEFORE_FLAG,
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, found)
        for excluded in NOT_COMMAND_LINE:
            with self.subTest(excluded=excluded):
                self.assertTrue(
                    (ROOT / excluded).is_file(), "an exclusion names a file the tree no longer has"
                )

    def test_discovery_finds_an_extensionless_shebang_script(self) -> None:
        """The hub tracks no such script yet, so a constructed tree stands in for the one that arrives."""
        files = {
            "bin/example": "#!/usr/bin/env python3\n",
            "bin/example-notes": "Example notes, not a script.\n",
            "tests/example": "#!/bin/sh\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            subprocess.run(["git", "-C", tmp, "add", "--", *files], check=True)
            self.assertEqual(entry_points(root), ["bin/example"])
            self.assertEqual(interpreter(root, "bin/example"), "python")


class SurvivorCase(unittest.TestCase):
    """The control for the survivor check: a detached grandchild is found and stopped."""

    @unittest.skipUnless(Path("/proc").is_dir(), "the survivor check reads /proc")
    def test_a_detached_grandchild_is_found_and_stopped(self) -> None:
        token = secrets.token_hex(16)
        env = dict(os.environ, **{RUN_TOKEN: token})
        detach = (
            "import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'], start_new_session=True, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
        )
        subprocess.run([sys.executable, "-c", detach], env=env, timeout=30, check=True)
        self.assertIn("left running:", stop_survivors(token))
        self.assertEqual(tagged_processes(token), [])


class DashLeadingNameCase(unittest.TestCase):
    """configure.sh hands resolve_description.py its arguments after `--`, so a leading dash stays a name."""

    def test_a_dash_leading_name_resolves_its_description(self) -> None:
        text = (ROOT / "repo-config" / "configure.sh").read_text(encoding="utf-8")
        found = re.findall(
            r'^    if ! description="\$\(.*?^    fi\n', text, re.MULTILINE | re.DOTALL
        )
        self.assertEqual(len(found), 1, "configure.sh no longer has one description resolution")
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "repos.json"
            registry.write_text(
                json.dumps({"repos": [{"name": "-Example", "description": "A short tagline."}]}),
                encoding="utf-8",
            )
            script = "\n".join(
                (
                    f"py_cmd=({shlex.quote(Path(sys.executable).as_posix())})",
                    f"script_dir={shlex.quote((ROOT / 'repo-config').as_posix())}",
                    f"registry={shlex.quote(registry.as_posix())}",
                    "name=-Example",
                    found[0],
                    'printf "%s" "$description"',
                )
            )
            result = subprocess.run(
                [bash_or_skip(), "-c", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "A short tagline.")


if __name__ == "__main__":
    unittest.main()
