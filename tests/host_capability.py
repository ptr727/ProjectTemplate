"""Host capabilities a test needs and a Windows host may lack.

A test that needs one of these skips with the reason when the host lacks it, rather than
erroring on a precondition that has nothing to do with the code under test.
"""

from __future__ import annotations

import functools
import ntpath
import os
import shutil
import tempfile
import unittest
from pathlib import Path


@functools.cache
def can_symlink() -> bool:
    """Whether this host lets an unprivileged process create a symlink.

    Windows refuses one with WinError 1314 unless Developer Mode is on or the process is elevated.
    """
    with tempfile.TemporaryDirectory() as tmp:
        try:
            (Path(tmp) / "link").symlink_to(Path(tmp) / "target")
        except (OSError, NotImplementedError):
            return False
    return True


NO_SYMLINK = "this host cannot create a symlink without Developer Mode or elevation"
requires_symlink = unittest.skipUnless(can_symlink(), NO_SYMLINK)


_WINDOWS = os.name == "nt"


def _windows_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def runs_a_script(found: str) -> bool:
    """Whether found, an absolute path, is a bash that runs a script on this host.

    On Windows that rules out a Windows-supplied bash.exe, under the Windows directory or among
    the WindowsApps aliases, which starts WSL rather than running bash here. Paths are compared by
    ntpath rather than resolved, so the check reads the same on any host a test runs it on.
    """
    if not _WINDOWS:
        return True
    path = _windows_path(found)
    windows = _windows_path(os.environ.get("SystemRoot", r"C:\Windows"))
    if path.startswith(windows + "\\"):
        return False
    profile = os.environ.get("LOCALAPPDATA")
    return not profile or ntpath.dirname(path) != _windows_path(
        ntpath.join(profile, "Microsoft", "WindowsApps")
    )


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def bash_path() -> str | None:
    """The first bash on PATH that a workflow step's script can run under, or None.

    A bare "bash" passed to subprocess on Windows is resolved by CreateProcess, which searches
    System32 before PATH and so finds the WSL launcher. That launcher hands the script to a second
    shell that re-parses it, which mangles any quoting or expansion the script relies on, so the
    PATH entry is named instead, passing over the launcher wherever PATH lists it. There each
    absolute directory is probed for bash.exe itself rather than through shutil.which, which on
    Windows would also look in the current directory first and admit a bash.cmd shim that cmd.exe
    re-parses. Elsewhere shutil.which answers, as it did for a bare "bash". PATH is read on every
    call rather than cached, since a test may change it for the duration of its run.

    A script holding a doubled backslash is handed to this bash as a file rather than through
    `-c`. subprocess writes the pair literally into the Windows command line, and Git Bash's own
    parse of that line collapses it to one, which a jq filter's escaped class then fails to compile.
    """
    if not _WINDOWS:
        return shutil.which("bash")
    for entry in os.environ.get("PATH", "").split(ntpath.pathsep):
        directory = entry.strip('"')
        if not ntpath.isabs(directory):
            continue
        candidate = ntpath.join(directory, "bash.exe")
        if _executable(candidate) and runs_a_script(candidate):
            return candidate
    return None


def bash_or_skip() -> str:
    """bash_path(), skipping the calling test where this host has none."""
    found = bash_path()
    if found is None:
        raise unittest.SkipTest(
            "no bash on PATH that runs a script on this host, so run the suite from Git Bash"
        )
    return found
