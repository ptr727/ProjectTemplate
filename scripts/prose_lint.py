#!/usr/bin/env python3
"""Run the action-owned prose gate locally, through uv so its script block's packages resolve.

The command line is read here first, from the data file the gate builds its own from, so `--help`
and an unknown argument are answered without starting uv.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / ".github" / "actions" / "prose-gate" / "prose_lint.py"


def main(argv: list[str]) -> int:
    data = json.loads(GATE.with_name("prose-gate.json").read_text(encoding="utf-8"))
    ap = argparse.ArgumentParser(prog="prose_lint.py")
    for flag, spec in data["cli"].items():
        choices = {"choices": sorted(data["rules"])} if spec.get("choices") == "rules" else {}
        ap.add_argument(flag, **{**spec, **choices})
    ap.parse_args(argv)
    uv = shutil.which("uv")
    if uv is None:
        print("prose_lint.py: uv is not on PATH, so the prose gate did not run.", file=sys.stderr)
        return 1
    return subprocess.run(
        [uv, "run", "--no-project", "--script", str(GATE), *argv], check=False
    ).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
