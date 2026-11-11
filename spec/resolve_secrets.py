#!/usr/bin/env python3
"""Resolve the secret names a fleet repo owes and the ~/.secrets/ file each is read from.

Two subcommands share one resolver, spec/secret_names.py, which spec/audit.py uses as well.
Both read a repo's tree on its registry ground branch, the branch the audit reads.

`names` is run by repo-config/configure.sh for one repo.
It reads that repo's tree with read-only gh calls.
The tree decides only whether a repo with tests owes the codecov mechanism.
It prints a JSON object with the rows (name, scope, file, stores), the forbidden names, and the strays.
A stray is the file a name would have at the wrong scope.
With `--live` it also reads each secret store and adds the findings of secret_names.compare_stores.
A ground branch that does not exist yet is a reported state rather than a failure.
Its JSON then carries empty lists and a `skip` string naming why, and the exit status is 0.
It exits 1 on a malformed registry, a duplicate or unknown entry, a name that has no scope, or any other gh failure.

`scaffold` walks the fleet and pre-creates every expected file as an empty mode 600 file in a mode 700 directory.
It reads each ground branch tree with read-only gh calls.
It never opens, changes, or deletes a file that exists.
A wrong-scope file found anywhere in the directory is reported and stops the run before anything is created.
A repo whose ground branch does not exist yet is listed under its own heading and the other repos are still scaffolded.
It then exits 1, so the run never reads as complete.

Usage: resolve_secrets.py names --slug OWNER/REPO [--live] [--] SECRETS_JSON REGISTRY NAME
       resolve_secrets.py scaffold [--secrets-dir DIR] [--dry-run]

Argparse reads a NAME starting with a dash as an option, so a caller passes `--` before the positionals.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import resolve_description
import secret_names
import validate  # sibling, import-safe (its main is guarded)

ROOT = Path(__file__).resolve().parent.parent


class ResolveError(Exception):
    """A condition the CLI must fail loud on."""


class GhStatusError(ResolveError):
    """A gh read GitHub answered with an error status, which `status` holds as the digits gh printed."""

    def __init__(self, message: str, status: str | None) -> None:
        super().__init__(message)
        self.status = status


class BranchMissing(ResolveError):
    """A ground branch that does not exist yet, which is a state to report and not a failure."""


def gh_graphql_module() -> Any:
    """`scripts/gh_graphql.py`, through validate.scripts_module, whose ROOT no test points elsewhere."""
    return validate.scripts_module("gh_graphql")


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
    live_reader: Callable[[str], dict[str, set[str]]] | None = None,
) -> dict[str, Any]:
    """The plan for NAME: its rows, forbidden names, and strays, from the tree of SLUG's ground branch.

    A ground branch that does not exist yet answers empty lists and a `skip` sentence, so the caller reports it.
    A live_reader adds the findings of comparing the live stores with what the repo owes.
    """
    entry = registry_entry(registry, name)
    # The entry is looked up by name alone, so a slug it does not name would write one repo's credentials into another.
    identity = validate.github_identity(entry.get("url"))
    if identity is None or identity.lower() != slug.lower():
        raise ResolveError(
            f"the registry entry {name} names {identity}, not {slug}. "
            "Refusing to resolve secrets for a repository the entry does not name"
        )
    try:
        tree = tree_reader(slug, ground_branch(entry, registry))
    except BranchMissing as e:
        return {"rows": [], "forbidden": [], "strays": [], "skip": str(e)}
    try:
        live = live_reader(slug) if live_reader else None
        return secret_names.secret_plan(secrets, entry, tree, name, live)
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
    """Every blob path on BRANCH of SLUG, raising when the tree cannot be read in full.

    A 404 or a 409 is how GitHub answers for a branch that is not there, or for an empty repo.
    Either raises BranchMissing, and no other failure does.
    """
    try:
        tree = gh_json(["gh", "api", f"repos/{slug}/git/trees/{branch}?recursive=1"], slug)
    except GhStatusError as e:
        if e.status in ("404", "409"):
            raise BranchMissing(f"the {branch} branch of {slug} does not exist yet") from e
        raise
    blobs = secret_names.tree_blobs(tree)
    if blobs is None:
        raise ResolveError(f"{slug}: the {branch} tree is unreadable or truncated")
    return set(blobs)


def gh_run(command: list[str], slug: str) -> str:
    """The stdout of a read-only gh call, raising with the repo named on any failure.

    The subprocess helper is the one scripts/gh_graphql.py gives the other scripts reading through gh.
    Its refusal to run gh at all, such as no gh on PATH, becomes the same loud failure as a failed read.
    """
    module = gh_graphql_module()
    try:
        code, out, err = module.run_subprocess(command)
    except module.GraphQLError as e:
        raise ResolveError(f"{slug}: {e}") from e
    if code != 0:
        status = module.HTTP_STATUS.search(err)
        raise GhStatusError(
            f"{slug}: {' '.join(command)} failed: {err.strip()}",
            status.group(1) if status else None,
        )
    return out


def gh_json(command: list[str], slug: str) -> Any:
    """The JSON a read-only gh call prints, whose shape the caller checks."""
    out = gh_run(command, slug)
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise ResolveError(f"{slug}: {' '.join(command)} printed no JSON: {e}") from e


def read_live_names(slug: str) -> dict[str, set[str]]:
    """The secret names each store holds, paginated, never a value."""
    return {
        store: set(
            gh_run(
                [
                    "gh",
                    "api",
                    "--paginate",
                    f"repos/{slug}/{store}/secrets",
                    "--jq",
                    ".secrets[].name",
                ],
                slug,
            ).split()
        )
        for store in secret_names.STORES
    }


def fleet_expectations(
    secrets: dict[str, Any], registry: Any, tree_reader: Callable[[str, str], set[str]]
) -> tuple[dict[str, list[str]], set[str], set[str], list[str]]:
    """The expected files mapped to their repos, the files held for skipped repos, every wrong-scope file, and the skipped repos.

    A repo whose ground branch does not exist yet is skipped with its reason, and the walk goes on.
    It is not scaffolded, but the files it owes are held, so its existing files never read as unclaimed.
    Its wrong-scope files count as strays too.

    Only cataloged entries are walked, since the audit covers only those and an excluded or archived one owes nothing.
    """
    if not isinstance(registry, dict) or not isinstance(registry.get("repos"), list):
        raise ResolveError("registry is not an object with a 'repos' array")
    expected: dict[str, list[str]] = {}
    held: set[str] = set()
    strays: set[str] = set()
    skipped: list[str] = []
    for entry in registry["repos"]:
        if not isinstance(entry, dict) or entry.get("status") != "cataloged":
            continue
        name = entry.get("name")
        slug = validate.github_identity(entry.get("url"))
        if not isinstance(name, str) or slug is None:
            raise ResolveError(f"registry entry {name!r} has no name or no GitHub url")
        try:
            try:
                tree: set[str] | None = tree_reader(slug, ground_branch(entry, registry))
                skip = None
            except BranchMissing as e:
                # Without a tree no test directory can be seen, and an unread tree is read as every candidate keeping its claim.
                # That is the right reading here, since claiming a file too many only keeps it from being called unclaimed.
                # Nothing is created from it, because only `expected` drives creation.
                tree, skip = None, str(e)
            plan = secret_names.secret_plan(secrets, entry, tree, name)
        except secret_names.SecretNameError as e:
            raise ResolveError(f"{name}: {e}") from e
        strays |= set(plan["strays"])
        if skip is not None:
            skipped.append(f"{name}: {skip}")
            held |= {row["file"] for row in plan["rows"]}
            continue
        for row in plan["rows"]:
            expected.setdefault(row["file"], []).append(name)
    return expected, held - set(expected), strays - set(expected) - held, skipped


def registry_short_names(registry: Any) -> set[str]:
    """Every `shortName` the registry holds, the owner tokens a consumer secret file opens with."""
    return {e["shortName"] for e in registry["repos"] if "shortName" in e}


def scaffold(
    secrets: dict[str, Any],
    registry: Any,
    directory: Path,
    dry_run: bool,
    tree_reader: Callable[[str, str], set[str]],
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Create each missing expected file, answering (created, present, strays, unclaimed, skipped).

    The first four are file names, and skipped holds one sentence per repo whose ground branch is missing.

    Every tree is read and every stray looked for before the first file is created.
    A listing of the directory names entries and opens none of them.
    A consumer file is `<shortName>_<purpose>[_<part>]`, so its first field is its owner's registry short name.
    The name is the ownership record, so a file whose first field is no short name is unclaimed.
    A `github_` file is a provisioning file.
    """
    expected, held, wrong_scope, skipped = fleet_expectations(secrets, registry, tree_reader)
    on_disk = set()
    if directory.is_dir():
        short_names = registry_short_names(registry)
        on_disk = {
            e.name
            for e in os.scandir(directory)
            if e.name.startswith(secret_names.FILE_PREFIX)
            or e.name.partition("_")[0] not in short_names
            or "_" not in e.name
        }
    strays = sorted(on_disk & wrong_scope)
    present = sorted(on_disk & (set(expected) | held))
    unclaimed = sorted(on_disk - set(expected) - held - wrong_scope)
    if strays:
        return [], present, strays, unclaimed, skipped
    created = sorted(set(expected) - on_disk)
    if created and not dry_run:
        if not directory.is_dir():
            directory.mkdir(mode=0o700)
        for file_name in created:
            # O_EXCL makes a file that appeared since the listing stop the run, rather than be counted as created here.
            os.close(os.open(directory / file_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    return created, present, strays, unclaimed, skipped


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
        plan = resolve_names(
            secrets,
            registry,
            args.name,
            args.slug,
            read_tree,
            read_live_names if args.live else None,
        )
    except ResolveError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps(plan))
    return 0


def run_scaffold(args: argparse.Namespace) -> int:
    directory = Path(args.secrets_dir) if args.secrets_dir else Path.home() / ".secrets"
    try:
        secrets = json.loads((ROOT / "spec" / "secrets.json").read_text(encoding="utf-8"))
        registry = json.loads((ROOT / "registry" / "repos.json").read_text(encoding="utf-8"))
        created, present, strays, unclaimed, skipped = scaffold(
            secrets, registry, directory, args.dry_run, read_tree
        )
    except (OSError, json.JSONDecodeError, ResolveError) as e:
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
    if skipped:
        print_list("Skipped, with no ground branch to read yet", skipped)
        print(
            "The files above cover the other repos. Run again once each branch exists.",
            file=sys.stderr,
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    names = commands.add_parser("names", help="resolve one repo's secret names from its tree")
    names.add_argument("--slug", metavar="OWNER/REPO", required=True)
    names.add_argument("--live", action="store_true", help="also compare the live secret stores")
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
