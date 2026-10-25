"""What a fresh interpreter imports, for the tests that hold a plain-python3 import path free of third-party libraries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# The shared Markdown reader and the library it imports, neither of which a plain-python3 importer may load.
MARKDOWN_READERS = ("fidelity_text", "markdown_it")


def import_delta(
    directory: Path, module: str, watched: tuple[str, ...] = MARKDOWN_READERS
) -> tuple[list[str], list[str]]:
    """Which of `watched` a fresh interpreter holds before and after importing `module` from `directory`."""
    names = list(watched)
    source = (
        f"import sys; names = {names!r}; before = [n for n in names if n in sys.modules];"
        f" sys.path.insert(0, {str(directory)!r}); import {module};"
        " print(','.join(before) + ' ' + ','.join(n for n in names if n in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    before, _, after = result.stdout.strip("\n").partition(" ")
    return [n for n in before.split(",") if n], [n for n in after.split(",") if n]
