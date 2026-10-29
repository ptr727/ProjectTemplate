#!/usr/bin/env python3
"""Resolve one repo's declared registry/repos.json description, for repo-config/configure.sh.

Delegates to description_errors() (validate.py) so configure.sh validates a declared description
against the exact same contract spec/audit_markdown.py's description_findings() does.
This avoids a third hand-rolled copy of the same rules.

Prints the declared description to stdout and exits 0 when the repo has no declared description
(nothing printed) or exactly one valid one.
Exits 1 with a message on stderr for anything configure.sh should fail loud on.
That is a malformed registry, more than one entry named NAME, or a declared description that
description_errors() rejects.

Usage: resolve_description.py [--] REGISTRY_PATH NAME

Argparse reads a NAME starting with a dash as an option, so a caller passes `--` before the
positionals, as configure.sh does.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import validate  # sibling, import-safe (its main is guarded)


class ResolveError(Exception):
    """A condition resolve_description() must fail loud on."""


def resolve_description(registry: dict, name: str) -> str | None:
    """The declared description for NAME in REGISTRY, or None if the repo has none declared.

    Raises ResolveError for anything the caller should fail loud on rather than silently read as
    absent:
    - a registry that is not an object carrying a `repos` array
    - an entry whose own name would match NAME once whitespace and case differences are normalized
      away but not otherwise
    - more than one entry named NAME
    - a declared description that description_errors() rejects

    The second case is raised rather than read as a different repo.
    spec/validate.py rejects a padded name outright. GitHub itself compares a repo name
    case-insensitively, so a same-name-different-case entry is a data-entry mistake rather than a
    different repo.
    """
    if not isinstance(registry, dict) or not isinstance(registry.get("repos"), list):
        raise ResolveError("registry is not an object with a 'repos' array")
    repos = registry["repos"]
    normalized_name = name.strip().casefold()
    near_miss = next(
        (
            r["name"]
            for r in repos
            if isinstance(r, dict)
            and isinstance(r.get("name"), str)
            and r["name"] != name
            and r["name"].strip().casefold() == normalized_name
        ),
        None,
    )
    if near_miss is not None:
        raise ResolveError(
            f"a registry entry's name {near_miss!r} differs from {name!r} only by whitespace or letter case"
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("registry_path", metavar="REGISTRY_PATH")
    parser.add_argument("name", metavar="NAME")
    args = parser.parse_args(argv)
    registry_path, name = args.registry_path, args.name
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
    sys.exit(main(sys.argv[1:]))
