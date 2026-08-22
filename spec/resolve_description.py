#!/usr/bin/env python3
"""Resolve one repo's declared registry/repos.json description, for repo-config/configure.sh.

Delegates to description_errors() (validate.py) so configure.sh validates a declared description
against the exact same contract spec/audit.py's description_findings() does, rather than a third
hand-rolled copy of the same rules.

Prints the declared description to stdout and exits 0 when the repo has no declared description
(nothing printed) or exactly one valid one. Exits 1 with a message on stderr for anything
configure.sh should fail loud on: a malformed registry, more than one entry named NAME, or a
declared description that description_errors() rejects.

Usage: resolve_description.py REGISTRY_PATH NAME
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import validate  # sibling, import-safe (its main is guarded)


class ResolveError(Exception):
    """A condition resolve_description() must fail loud on."""


def resolve_description(registry: dict, name: str) -> str | None:
    """The declared description for NAME in REGISTRY, or None if the repo has none declared.

    Raises ResolveError for anything the caller should fail loud on rather than silently read as
    absent: a registry that is not an object carrying a `repos` array, an entry whose own name
    would match NAME but for leading/trailing whitespace (spec/validate.py rejects that shape too,
    so it is never the intended way to spell a mismatch), more than one entry named NAME, or a
    declared description description_errors() rejects.
    """
    if not isinstance(registry, dict) or not isinstance(registry.get("repos"), list):
        raise ResolveError("registry is not an object with a 'repos' array")
    repos = registry["repos"]
    near_miss = next(
        (
            r["name"]
            for r in repos
            if isinstance(r, dict)
            and isinstance(r.get("name"), str)
            and r["name"] != name
            and r["name"].strip() == name
        ),
        None,
    )
    if near_miss is not None:
        raise ResolveError(
            f"a registry entry's name {near_miss!r} carries leading/trailing whitespace"
        )
    matches = [r for r in repos if isinstance(r, dict) and r.get("name") == name]
    if len(matches) > 1:
        raise ResolveError(
            f"{len(matches)} registry entries named {name}. "
            "Resolve the duplicate before its description can be read"
        )
    if not matches or "description" not in matches[0]:
        return None
    desc = matches[0]["description"]
    errors = validate.description_errors(name, desc)
    if errors:
        raise ResolveError("; ".join(errors))
    return desc


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
    try:
        desc = resolve_description(registry, name)
    except ResolveError as e:
        print(f"{e} (spec/validate.py rejects this once run).", file=sys.stderr)
        return 1
    if desc is not None:
        sys.stdout.write(desc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
