#!/usr/bin/env python3
"""Run the action-owned prose gate implementation locally."""

from pathlib import Path
from runpy import run_path

run_path(
    str(Path(__file__).resolve().parents[1] / ".github/actions/prose-gate/prose_lint.py"),
    run_name="__main__",
)
