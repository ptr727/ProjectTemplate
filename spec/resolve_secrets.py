#!/usr/bin/env python3
"""Resolve the secret names a fleet repo owes and the ~/.secrets/ file each is read from.

Two subcommands share one resolver, spec/secret_names.py, which spec/audit.py uses as well.
Both read a repo's tree on its registry ground branch, the branch the audit reads.

`names` is run by repo-config/configure.sh for one repo.
It reads that repo's tree with read-only gh calls, since the tree decides whether a repo owes a Codecov token.
It prints a JSON object with the rows (name, scope, file, stores), the forbidden names, and the strays.
A stray is the file a name would have at the wrong scope.
It exits 1 on a malformed registry, a duplicate or unknown entry, or a name that has no scope.

`scaffold` walks the fleet and pre-creates every expected file as an empty mode 600 file in a mode 700 directory.
It reads each ground branch tree with read-only gh calls.
It never opens, changes, or deletes a file that exists.
A wrong-scope file found anywhere in the directory is reported and stops the run before anything is created.

Usage: resolve_secrets.py names --slug OWNER/REPO [--] SECRETS_JSON REGISTRY NAME
       resolve_secrets.py scaffold [--secrets-dir DIR] [--dry-run]

Argparse reads a NAME starting with a dash as an option, so a caller passes `--` before the positionals.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import resolve_description
import secret_names
import validate  # sibling, import-safe (its main is guarded)

ROOT = Path(__file__).resolve().parent.parent

FILE_PREFIX = "github_"


class ResolveError(Exception):
    """A condition the CLI must fail loud on."""


def registry_entry(registry: Any, name: str) -> dict[str, Any]:
    """The one registry entry named NAME, raising when there is none or the lookup is unsound.

    The lookup is resolve_description.registry_entry(), so a malformed registry, a duplicate, and a
    near-miss on case or whitespace fail the same way for both.
    """
    try:
        entry = resolve_description.registry_entry(registry, name)
    except resolve_description.ResolveError as e:
        raise ResolveError(str(e)) from e
    if entry is None:
        raise ResolveError(f"no registry entry named {name}")
    return entry


def resolve_names(
    secrets: dict[str, Any],
    registry: Any,
    name: str,
    slug: str,
    tree_reader: Callable[[str, str], set[str]],
) -> dict[str, Any]:
    """The plan for NAME: its rows, forbidden names, and strays, from the tree of SLUG's ground branch."""
    entry = registry_entry(registry, name)
    tree = tree_reader(slug, ground_branch(entry, registry))
    try:
        return secret_names.secret_plan(secrets, entry, tree, name)
    except secret_names.SecretNameError as e:
        raise ResolveError(str(e)) from e


def ground_branch(entry: dict[str, Any], registry: Any) -> str:
    """The ground branch the audit reads for ENTRY, raising when the registry declares an unusable one."""
    defaults = registry.get("defaults") if isinstance(registry, dict) else None
    branch = secret_names.ground_branch_of(
        entry, None, defaults if isinstance(defaults, dict) else None
    )
    if not isinstance(branch, str) or not branch.strip():
        raise ResolveError(f"{entry.get('name')}: groundTruthBranch is not a non-empty string")
    return branch


def read_tree(slug: str, branch: str) -> set[str]:
    """Every blob path on BRANCH of SLUG, raising when the tree cannot be read in full."""
    tree = gh_json(["gh", "api", f"repos/{slug}/git/trees/{branch}?recursive=1"], slug)
    if not isinstance(tree.get("tree"), list) or tree.get("truncated"):
        raise ResolveError(f"{slug}: the {branch} tree is unreadable or truncated")
    return {n["path"] for n in tree["tree"] if n.get("type") == "blob"}


def gh_json(command: list[str], slug: str) -> dict[str, Any]:
    """The JSON object a read-only gh call prints, raising with the repo named on any failure."""
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=120
    )
    if result.returncode != 0:
        raise ResolveError(f"{slug}: {' '.join(command)} failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ResolveError(f"{slug}: {' '.join(command)} printed no JSON: {e}") from e
    if not isinstance(data, dict):
        raise ResolveError(f"{slug}: {' '.join(command)} printed a non-object")
    return data


def fleet_expectations(
    secrets: dict[str, Any], registry: Any, tree_reader: Callable[[str, str], set[str]]
) -> tuple[dict[str, list[str]], set[str]]:
    """The expected file names mapped to the repos expecting each, and every wrong-scope file name.

    Archived entries are skipped, since GitHub rejects a write to one.
    """
    if not isinstance(registry, dict) or not isinstance(registry.get("repos"), list):
        raise ResolveError("registry is not an object with a 'repos' array")
    expected: dict[str, list[str]] = {}
    strays: set[str] = set()
    for entry in registry["repos"]:
        if not isinstance(entry, dict) or entry.get("status") == "archived":
            continue
        name = entry.get("name")
        slug = validate.github_identity(entry.get("url"))
        if not isinstance(name, str) or slug is None:
            raise ResolveError(f"registry entry {name!r} has no name or no GitHub url")
        try:
            tree = tree_reader(slug, ground_branch(entry, registry))
            plan = secret_names.secret_plan(secrets, entry, tree, name)
        except secret_names.SecretNameError as e:
            raise ResolveError(f"{name}: {e}") from e
        for row in plan["rows"]:
            expected.setdefault(row["file"], []).append(name)
        strays |= set(plan["strays"])
    return expected, strays - set(expected)


def directory_problem(directory: Path) -> str | None:
    """Why DIRECTORY cannot hold secrets, or None where it is absent or private."""
    try:
        info = directory.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return f"{directory} exists and is not a directory"
    if info.st_mode & 0o077:
        return (
            f"{directory} is mode {stat.S_IMODE(info.st_mode):o}, "
            "and must give group and other no access"
        )
    return None


def scaffold(
    secrets: dict[str, Any],
    registry: Any,
    directory: Path,
    dry_run: bool,
    tree_reader: Callable[[str, str], set[str]],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Create each missing expected file, answering (created, present, strays, unclaimed) file names.

    Every tree is read and every stray looked for before the first file is created.
    A listing of the directory names entries and opens none of them.
    """
    problem = directory_problem(directory)
    if problem:
        raise ResolveError(problem)
    expected, wrong_scope = fleet_expectations(secrets, registry, tree_reader)
    on_disk = set()
    if directory.is_dir():
        on_disk = {e.name for e in os.scandir(directory) if e.name.startswith(FILE_PREFIX)}
    strays = sorted(on_disk & wrong_scope)
    present = sorted(on_disk & set(expected))
    unclaimed = sorted(on_disk - set(expected) - wrong_scope)
    if strays:
        return [], present, strays, unclaimed
    created = sorted(set(expected) - on_disk)
    if created and not dry_run:
        if not directory.is_dir():
            directory.mkdir(mode=0o700)
        for file_name in created:
            # O_EXCL makes a file that appeared since the listing a stop rather than an overwrite.
            os.close(os.open(directory / file_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    return created, present, strays, unclaimed


def print_list(title: str, items: list[str]) -> None:
    print(f"{title} ({len(items)}):")
    for item in items:
        print(f"  {item}")


def run_names(args: argparse.Namespace) -> int:
    try:
        secrets = json.loads(Path(args.secrets_json).read_text(encoding="utf-8"))
        registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Failed to read the secrets spec or the registry: {e}", file=sys.stderr)
        return 1
    try:
        plan = resolve_names(secrets, registry, args.name, args.slug, read_tree)
    except (ResolveError, subprocess.TimeoutExpired) as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps(plan))
    return 0


def run_scaffold(args: argparse.Namespace) -> int:
    directory = Path(args.secrets_dir) if args.secrets_dir else Path.home() / ".secrets"
    try:
        secrets = json.loads((ROOT / "spec" / "secrets.json").read_text(encoding="utf-8"))
        registry = json.loads((ROOT / "registry" / "repos.json").read_text(encoding="utf-8"))
        created, present, strays, unclaimed = scaffold(
            secrets, registry, directory, args.dry_run, read_tree
        )
    except (OSError, json.JSONDecodeError, ResolveError, subprocess.TimeoutExpired) as e:
        print(f"Scaffold stopped: {e}", file=sys.stderr)
        return 1
    if strays:
        print_list("Wrong-scope files found, not read and not deleted", strays)
        print(
            "Each is either a bad secret file or a scope that changed in spec/secrets.json. "
            "Nothing was created.",
            file=sys.stderr,
        )
        return 1
    print_list("Would create" if args.dry_run else "Created", created)
    print_list("Already present, not opened", present)
    print_list("Unclaimed by any repo, left alone", unclaimed)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    names = commands.add_parser("names", help="resolve one repo's secret names from its tree")
    names.add_argument("--slug", metavar="OWNER/REPO", required=True)
    names.add_argument("secrets_json", metavar="SECRETS_JSON")
    names.add_argument("registry", metavar="REGISTRY")
    names.add_argument("name", metavar="NAME")
    names.set_defaults(run=run_names)
    scaffolding = commands.add_parser("scaffold", help="pre-create every expected secret file")
    scaffolding.add_argument("--secrets-dir", metavar="DIR", default="")
    scaffolding.add_argument("--dry-run", action="store_true")
    scaffolding.set_defaults(run=run_scaffold)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
