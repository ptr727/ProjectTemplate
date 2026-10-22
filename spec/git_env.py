"""The environment variables that name a git repository, and the ways to run without them.

A git hook sets some of them (`GIT_DIR`, `GIT_INDEX_FILE`, and the like). A script or test that
builds a temporary repository with `git -C <tmp>` or `cwd=<tmp>` and inherits them builds it
inside the hook's own repository instead, and reads its answers back from there.

The list is what `git rev-parse --local-env-vars` prints.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping

GIT_LOCATION_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


def without_git_location(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of `env` (default: the process environment) with every location variable removed."""
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if k not in GIT_LOCATION_VARS}


@contextlib.contextmanager
def git_location_cleared() -> Iterator[None]:
    """Remove every location variable from the process environment, restoring them on exit."""
    cleared = {name: os.environ.pop(name) for name in GIT_LOCATION_VARS if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(cleared)
