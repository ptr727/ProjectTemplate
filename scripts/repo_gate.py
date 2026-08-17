#!/usr/bin/env python3
"""Run the action-owned repository gate implementation locally."""

from pathlib import Path
from runpy import run_path

run_path(
    str(Path(__file__).resolve().parents[1] / ".github/actions/repo-gate/repo_gate.py"),
    run_name="__main__",
)
