"""The .NET SDK line the fleet's global.json baseline selects, read the one way every test holding a copy of it reads it."""

from __future__ import annotations

import json
from pathlib import Path

BASELINE = Path(__file__).resolve().parents[1] / "catalog" / "snippets" / "configs" / "global.json"


def baseline_line() -> tuple[int, int]:
    """The major and minor of the SDK line the baseline's `sdk.version` selects."""
    version = json.loads(BASELINE.read_text(encoding="utf-8"))["sdk"]["version"]
    major, minor = version.split(".")[:2]
    return int(major), int(minor)
