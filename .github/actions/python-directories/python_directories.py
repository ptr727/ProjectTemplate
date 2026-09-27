#!/usr/bin/env python3
"""Resolve the Python directories the validator gates, from the caller's declaration.

A caller declares each directory holding a Python project in validate-task.yml's
`python-directories` input, one repository-relative path per line. A caller declaring none
keeps the root, where a root pyproject.toml is tracked, which is the only directory the
validator gated before the input existed.

The declaration is what decides what is gated, never discovery, since a stray or vendored
`.py` file would otherwise change the gate silently. Tracked `.py` files that no resolved
directory covers are therefore reported as a warning rather than gated, and the fleet audit,
which compares the declaration against the registry, is where they become a finding.

Each directory is classed by the shape that decides how its tools run: `uv` for a tracked
uv.lock of its own, `pip` for a requirements*.txt beside it, `lint-only` for a pyproject.toml
with no [project] or [build-system] table, and `unsupported` for a project with none of those,
whose dependencies the validator cannot install. A uv workspace member is that last shape,
since its lock belongs to the workspace root, which is the directory to declare instead.

Writes `projects` (one `<shape><TAB><directory>` line each), `any`, and `declared` to
GITHUB_OUTPUT. Exits 1 on a declaration naming a path that is not a tracked Python project.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

UNCOVERED_SHOWN = 10
PROJECT_TABLE = re.compile(r"^[ \t]*\[(project|build-system)\]", re.MULTILINE)


def escape_command(value: str) -> str:
    """Encode a value for a workflow command, percent first so the later escapes are not re-encoded."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
    return [path for path in result.stdout.decode("utf-8", "surrogateescape").split("\0") if path]


def normalize(line: str) -> tuple[str | None, str | None]:
    """The declared directory in canonical form, or the reason it is refused."""
    path = line.strip()
    if not path:
        return None, None
    if "\t" in path:
        return None, f"'{path}' must not hold a tab"
    if path.startswith("/") or "\\" in path:
        return None, f"'{path}' must be a repository-relative path with forward slashes"
    if ".." in path.split("/"):
        return None, f"'{path}' must not step outside the repository"
    return posixpath.normpath(path), None


def covers(directory: str, path: str) -> bool:
    return directory == "." or path.startswith(directory + "/")


def resolve(declared_text: str, tracked: list[str]) -> tuple[list[str], bool, list[str]]:
    """The directories to gate, whether the caller declared them, and the errors refusing the declaration."""
    tracked_set = set(tracked)
    directories: list[str] = []
    errors: list[str] = []
    for line in declared_text.splitlines():
        directory, error = normalize(line)
        if error:
            errors.append(error)
        if directory is None or directory in directories:
            continue
        project = "pyproject.toml" if directory == "." else f"{directory}/pyproject.toml"
        if project not in tracked_set:
            errors.append(
                f"'{directory}' holds no tracked pyproject.toml, so it is not a Python project"
            )
            continue
        directories.append(directory)
    if errors or directories:
        return directories, True, errors
    return (["."] if "pyproject.toml" in tracked_set else []), False, []


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def in_directory(directory: str, name: str) -> str:
    return name if directory == "." else f"{directory}/{name}"


def shape(directory: str, tracked_set: set[str], read: Callable[[str], str]) -> str:
    """How the directory's tools run, from its tracked files and its pyproject.toml."""
    if in_directory(directory, "uv.lock") in tracked_set:
        return "uv"
    prefix = in_directory(directory, "requirements")
    if any(
        path.startswith(prefix) and path.endswith(".txt") and "/" not in path[len(prefix) :]
        for path in tracked_set
    ):
        return "pip"
    if not PROJECT_TABLE.search(read(in_directory(directory, "pyproject.toml"))):
        return "lint-only"
    return "unsupported"


def uncovered(directories: list[str], tracked: list[str]) -> list[str]:
    return [
        path
        for path in tracked
        if path.endswith(".py") and not any(covers(d, path) for d in directories)
    ]


def main() -> int:
    tracked = tracked_files()
    directories, declared, errors = resolve(os.environ.get("DECLARED", ""), tracked)
    if errors:
        for error in errors:
            print(f"::error::python-directories: {escape_command(error)}")
        return 1
    if os.environ.get("REPORT_UNCOVERED") == "true":
        missed = uncovered(directories, tracked)
        if missed:
            shown = ", ".join(missed[:UNCOVERED_SHOWN])
            more = (
                f" and {len(missed) - UNCOVERED_SHOWN} more"
                if len(missed) > UNCOVERED_SHOWN
                else ""
            )
            print(
                "::warning::"
                + escape_command(
                    f"{len(missed)} tracked .py file(s) sit outside every gated Python directory, so no Python gate "
                    f"reads them: {shown}{more}. Declare each project's directory in the python-directories input."
                )
            )
    tracked_set = set(tracked)
    projects = []
    for directory in directories:
        projects.append((shape(directory, tracked_set, read_file), directory))
        print(f"gating Python directory: {directory} ({projects[-1][0]})")
    if not directories:
        print("no Python directory to gate")
    # A random delimiter, since a fixed one could appear as a directory name and end the value early.
    delimiter = f"EOF_{uuid.uuid4().hex}"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"projects<<{delimiter}\n")
        output.writelines(f"{kind}\t{directory}\n" for kind, directory in projects)
        output.write(f"{delimiter}\n")
        output.write(f"any={'true' if directories else 'false'}\n")
        output.write(f"declared={'true' if declared else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
