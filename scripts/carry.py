#!/usr/bin/env python3
"""Check or apply manifest-owned trees from the hub to a fleet worktree."""

import argparse
import fnmatch
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent


class CarryError(RuntimeError):
    """A state that prevents a safe carry decision."""


@dataclass(frozen=True)
class Inventory:
    files: dict[str, bytes]
    directories: frozenset[str]
    digest: str


def load_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CarryError(f"cannot read {path}: {exc}") from exc


def relative_root(root: pathlib.Path, value: str) -> pathlib.Path:
    declared = pathlib.PurePosixPath(value)
    if declared == pathlib.PurePosixPath(".") or declared.is_absolute() or ".." in declared.parts:
        raise CarryError(
            f"path must be repository-relative and below the repository root without '..': {value}"
        )
    resolved_root = root.resolve()
    candidate = resolved_root / value
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise CarryError(f"path escapes repository root: {value}") from exc
    current = resolved_root
    for part in pathlib.PurePosixPath(value).parts:
        current /= part
        if current.is_symlink():
            raise CarryError(f"symlink is not allowed: {current}")
    return candidate


def included(path: str, patterns: list[str]) -> bool:
    return any(
        pattern == "**/*"
        or fnmatch.fnmatchcase(path, pattern)
        or pathlib.PurePosixPath(path).match(pattern)
        for pattern in patterns
    )


def inventory(root: pathlib.Path, patterns: list[str]) -> Inventory:
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    if root.is_symlink():
        raise CarryError(f"symlink is not allowed: {root}")
    if not root.exists():
        raise CarryError(f"tree root does not exist: {root}")
    if not root.is_dir():
        raise CarryError(f"tree root is not a directory: {root}")
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = pathlib.Path(current)
        for name in [*dirnames, *filenames]:
            path = current_path / name
            if path.is_symlink():
                raise CarryError(f"symlink is not allowed: {path}")
        relative_dir = current_path.relative_to(root).as_posix()
        if (
            relative_dir != "."
            and not dirnames
            and not filenames
            and included(relative_dir + "/placeholder", patterns)
        ):
            directories.add(relative_dir)
        for name in filenames:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if included(relative, patterns):
                try:
                    files[relative] = path.read_bytes()
                except OSError as exc:
                    raise CarryError(f"cannot read {path}: {exc}") from exc
    digest = hashlib.sha256()
    for relative in sorted(directories):
        digest.update(b"directory\0")
        digest.update(relative.encode())
        digest.update(b"\0")
    for relative, content in sorted(files.items()):
        digest.update(b"file\0")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return Inventory(files, frozenset(directories), digest.hexdigest())


def compare(source: Inventory, target: Inventory | None) -> dict[str, Any]:
    target_files = {} if target is None else target.files
    target_directories = frozenset() if target is None else target.directories
    return {
        "missing": sorted(set(source.files) - set(target_files)),
        "missingDirectories": sorted(source.directories - target_directories),
        "modified": sorted(
            path
            for path in set(source.files) & set(target_files)
            if source.files[path] != target_files[path]
        ),
        "extra": sorted(set(target_files) - set(source.files)),
        "extraDirectories": sorted(target_directories - source.directories),
        "sourceDigest": source.digest,
        "targetDigest": None if target is None else target.digest,
        "missingRoot": target is None,
    }


def apply_tree(
    source: Inventory,
    target_root: pathlib.Path,
    repository_root: pathlib.Path,
    result: dict[str, Any],
) -> list[str]:
    changes: list[str] = []
    required_directories = set(source.directories)
    for relative in [*source.files, *source.directories]:
        required_directories.update(
            str(parent) for parent in pathlib.PurePosixPath(relative).parents if str(parent) != "."
        )

    def remove_empty_ancestors(directory: pathlib.Path) -> None:
        while directory != target_root and directory.exists() and not any(directory.iterdir()):
            relative = directory.relative_to(target_root).as_posix()
            if relative in required_directories:
                return
            directory.rmdir()
            changes.append(f"remove {directory.relative_to(repository_root)}")
            directory = directory.parent

    created_roots = []
    current = target_root
    while current != repository_root and not current.exists():
        created_roots.append(current)
        current = current.parent
    target_root.mkdir(parents=True, exist_ok=True)
    changes.extend(
        f"create {directory.relative_to(repository_root)}" for directory in reversed(created_roots)
    )
    for relative in result["missingDirectories"]:
        destination = target_root / relative
        destination.mkdir(parents=True, exist_ok=True)
        changes.append(f"create {destination.relative_to(repository_root)}")
    for relative in [*result["missing"], *result["modified"]]:
        destination = target_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.files[relative])
        changes.append(f"write {destination.relative_to(repository_root)}")
    for relative in result["extra"]:
        destination = target_root / relative
        destination.unlink()
        changes.append(f"remove {destination.relative_to(repository_root)}")
        remove_empty_ancestors(destination.parent)
    for relative in sorted(
        result["extraDirectories"],
        key=lambda value: len(pathlib.PurePosixPath(value).parts),
        reverse=True,
    ):
        directory = target_root / relative
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
            changes.append(f"remove {directory.relative_to(repository_root)}")
            remove_empty_ancestors(directory.parent)
    return changes


def git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CarryError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def git_is_ancestor(root: pathlib.Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise CarryError(result.stderr.strip() or "git merge-base --is-ancestor failed")
    return result.returncode == 0


def normalized_origin(value: str) -> str:
    value = value.strip().rstrip("/").removesuffix(".git")
    if value.startswith("git@github.com:"):
        return "https://github.com/" + value.removeprefix("git@github.com:")
    if value.startswith("ssh://git@github.com/"):
        return "https://github.com/" + value.removeprefix("ssh://git@github.com/")
    return value


def verify_hub(hub: pathlib.Path, registry: dict[str, Any]) -> str:
    entry = resolve_repo("ProjectTemplate", registry)
    origin = normalized_origin(git(hub, "config", "--get", "remote.origin.url"))
    if origin != normalized_origin(entry["url"]):
        raise CarryError("hub origin does not match the ProjectTemplate registry entry")
    git(hub, "fetch", "origin", "main")
    head = git(hub, "rev-parse", "HEAD")
    if head != git(hub, "rev-parse", "origin/main"):
        raise CarryError("hub checkout is not at freshly fetched origin/main")
    if git(hub, "status", "--porcelain"):
        raise CarryError("hub checkout has local changes")
    return head


def applicable(selector: str | list[str], values: set[str]) -> bool:
    if selector == "*":
        return True
    tokens = selector if isinstance(selector, list) else [selector]
    return bool(set(tokens) & values)


def validate_declarations(declarations: list[dict[str, Any]], hub: pathlib.Path) -> None:
    for index, left in enumerate(declarations):
        if left.get("fidelity") != "verbatim-tree":
            raise CarryError(f"tree declaration has unsupported fidelity: {left.get('fidelity')}")
        relative_root(hub, left["source"])
        relative_root(hub, left["target"])
        for right in declarations[index + 1 :]:
            left_target = pathlib.PurePosixPath(left["target"])
            right_target = pathlib.PurePosixPath(right["target"])
            overlaps = (
                left_target == right_target
                or left_target in right_target.parents
                or right_target in left_target.parents
            )
            if overlaps:
                raise CarryError(
                    f"overlapping tree declarations have conflicting ownership: {left_target} and {right_target}"
                )


def resolve_repo(name: str, registry: dict[str, Any]) -> dict[str, Any]:
    matches = [entry for entry in registry.get("repos", []) if entry.get("name") == name]
    if len(matches) != 1:
        raise CarryError(f"repository is not uniquely registered: {name}")
    return matches[0]


def verify_target(
    target: pathlib.Path, entry: dict[str, Any], owned_roots: list[pathlib.Path]
) -> None:
    top = pathlib.Path(git(target, "rev-parse", "--show-toplevel")).resolve()
    if top != target.resolve():
        raise CarryError(f"target must name the repository root: {target}")
    if normalized_origin(git(target, "config", "--get", "remote.origin.url")) != normalized_origin(
        entry["url"]
    ):
        raise CarryError("target origin does not match the registry entry")
    branch = git(target, "branch", "--show-current")
    if not branch or branch in {"main", "develop"}:
        raise CarryError("target must be an isolated feature-branch worktree")
    if not (target / ".git").is_file():
        raise CarryError("target must be a linked worktree, not the primary checkout")
    git(target, "fetch", "origin", "develop")
    if not git_is_ancestor(target, "origin/develop", "HEAD"):
        raise CarryError("target branch must contain the current origin/develop head")
    worktree_rows = git(target, "worktree", "list", "--porcelain").splitlines()
    if sum(row == f"worktree {target.resolve()}" for row in worktree_rows) != 1:
        raise CarryError("target is not a registered git worktree")
    dirty = []
    for row in git(target, "status", "--porcelain", "--untracked-files=all").splitlines():
        relative = row[3:].split(" -> ")[-1]
        path = (target / relative).resolve(strict=False)
        if not any(path == root or root in path.parents for root in owned_roots):
            dirty.append(relative)
    if dirty:
        raise CarryError(f"target has unrelated changes: {', '.join(sorted(dirty))}")


def run(mode: str, name: str, target: pathlib.Path, hub: pathlib.Path = ROOT) -> int:
    registry = load_json(hub / "registry/repos.json")
    manifest = load_json(hub / "spec/files.json")
    hub_commit = verify_hub(hub, registry)
    entry = resolve_repo(name, registry)
    defaults = registry.get("defaults", {})
    selectors = set(entry.get("types", []))
    selectors.add(entry.get("workflowModel") or defaults.get("workflowModel") or "release")
    selectors.add(entry.get("releaseTrigger") or defaults.get("releaseTrigger") or "two-phase")
    if entry.get("consumerModel"):
        selectors.add(entry["consumerModel"])
    declarations = [
        declaration
        for declaration in manifest.get("trees", [])
        if applicable(declaration.get("appliesTo", "*"), selectors)
    ]
    validate_declarations(declarations, hub)
    if name == "ProjectTemplate" and any(
        not item.get("allowHubTarget", False) for item in declarations
    ):
        raise CarryError("a declaration does not allow ProjectTemplate as its target")
    owned_roots = [relative_root(target, item["target"]) for item in declarations]
    verify_target(target, entry, owned_roots)
    print(f"hubCommit: {hub_commit}")
    print(f"repository: {name}")
    print(f"types: {','.join(entry.get('types', []))}")
    print(f"declarations: {len(declarations)}")
    clean = True
    for declaration in declarations:
        source_root = relative_root(hub, declaration["source"])
        target_root = relative_root(target, declaration["target"])
        source = inventory(source_root, declaration["include"])
        target_patterns = ["**/*"] if declaration.get("prune") else declaration["include"]
        target_inventory = inventory(target_root, target_patterns) if target_root.exists() else None
        result = compare(source, target_inventory)
        if not declaration.get("prune"):
            result["extra"] = []
            result["extraDirectories"] = []
        print(
            json.dumps(
                {"source": declaration["source"], "target": declaration["target"], **result},
                sort_keys=True,
            )
        )
        declaration_clean = (
            not any(
                result[key]
                for key in (
                    "missing",
                    "missingDirectories",
                    "modified",
                    "extra",
                    "extraDirectories",
                )
            )
            and not result["missingRoot"]
        )
        clean = clean and declaration_clean
        if mode == "apply" and not declaration_clean:
            for change in apply_tree(source, target_root, target, result):
                print(change)
            final = compare(source, inventory(target_root, target_patterns))
            print(
                json.dumps(
                    {
                        "source": declaration["source"],
                        "target": declaration["target"],
                        "postApply": True,
                        **final,
                    },
                    sort_keys=True,
                )
            )
            if (
                any(
                    final[key]
                    for key in (
                        "missing",
                        "missingDirectories",
                        "modified",
                        "extra",
                        "extraDirectories",
                    )
                )
                or final["missingRoot"]
            ):
                raise CarryError(f"post-apply comparison failed for {declaration['target']}")
    return 0 if mode == "apply" or clean else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply"))
    parser.add_argument("repository")
    parser.add_argument("--target", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        return run(args.mode, args.repository, args.target.resolve())
    except CarryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
