#!/usr/bin/env python3
"""Fetch a ptr727/ProjectTemplate script fresh from `main` and run it in-process.

Never pinned or vendored: a pin nothing keeps current goes stale by construction, and CI
(this repo's own, and the hub's) is the backstop for a change that lands broken on `main`.
A fetch failure fails the caller loudly rather than silently skipping the gate it guards.
Usage: hub-fetch-run.py <hub-relative-path> [script-args...]
Example: hub-fetch-run.py .github/actions/prose-gate/prose_lint.py . --diff HEAD
"""

from __future__ import annotations

import runpy
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HUB_RAW_BASE = "https://raw.githubusercontent.com/ptr727/ProjectTemplate/main"


def main(argv: list[str]) -> int:
    if not argv:
        print("hub-fetch-run: usage: hub-fetch-run.py <hub-relative-path> [script-args...]", file=sys.stderr)
        return 2
    hub_path, script_args = argv[0], argv[1:]
    url = f"{HUB_RAW_BASE}/{hub_path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed https host
            content = response.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"hub-fetch-run: could not fetch {url}: {exc}", file=sys.stderr)
        print("hub-fetch-run: the gate did not run.", file=sys.stderr)
        return 1
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as handle:
        handle.write(content)
        tmp_path = Path(handle.name)
    old_argv = sys.argv
    try:
        sys.argv = [str(tmp_path), *script_args]
        try:
            runpy.run_path(str(tmp_path), run_name="__main__")
        except SystemExit as exc:
            if exc.code is None:
                return 0
            return exc.code if isinstance(exc.code, int) else 1
        return 0
    finally:
        sys.argv = old_argv
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
