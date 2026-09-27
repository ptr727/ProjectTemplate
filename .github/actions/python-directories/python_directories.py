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

Writes `directories` (newline-separated), `any`, and `declared` to GITHUB_OUTPUT. Exits 1 on
a declaration naming a path that is not a tracked Python project.
"""

from __future__ import annotations

import os
import posixpath
import subprocess
import sys
import uuid

UNCOVERED_SHOWN = 10


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
    for directory in directories:
        print(f"gating Python directory: {directory}")
    if not directories:
        print("no Python directory to gate")
    # A random delimiter, since a fixed one could appear as a directory name and end the value early.
    delimiter = f"EOF_{uuid.uuid4().hex}"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"directories<<{delimiter}\n")
        output.writelines(f"{directory}\n" for directory in directories)
        output.write(f"{delimiter}\n")
        output.write(f"any={'true' if directories else 'false'}\n")
        output.write(f"declared={'true' if declared else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
