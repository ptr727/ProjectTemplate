#!/usr/bin/env python3
"""Resolve one repo's declared registry/repos.json description, for repo-config/configure.sh.

Delegates to description_errors() (validate.py) so configure.sh validates a declared description
against the exact same contract spec/audit.py's description_findings() does, rather than a third
hand-rolled copy of the same rules.

Prints the declared description to stdout and exits 0 when the repo has no declared description
(nothing printed) or exactly one valid one. Exits 1 with a message on stderr for anything
configure.sh should fail loud on: a malformed registry, more than one entry named NAME, or a
declared description description_errors() rejects.

Usage: resolve_description.py REGISTRY_PATH NAME
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import validate  # sibling, import-safe (its main is guarded)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: resolve_description.py REGISTRY_PATH NAME", file=sys.stderr)
        return 1
    registry_path, name = sys.argv[1], sys.argv[2]
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Failed to read {registry_path}: {e}", file=sys.stderr)
        return 1
    matches = [
        r for r in registry.get("repos", []) if isinstance(r, dict) and r.get("name") == name
    ]
    if len(matches) > 1:
        print(
            f"{len(matches)} registry entries named {name} in {registry_path}. Resolve the "
            "duplicate before its description can be read (spec/validate.py rejects this once run).",
            file=sys.stderr,
        )
        return 1
    if not matches or "description" not in matches[0]:
        return 0
    desc = matches[0]["description"]
    errors = validate.description_errors(name, desc)
    if errors:
        for msg in errors:
            print(f"{msg} (spec/validate.py rejects this once run).", file=sys.stderr)
        return 1
    sys.stdout.write(desc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
