"""The ecosystems a dependabot.yml declares, and the Python ones a tree's manifests imply.

A language ecosystem is directory-scoped, so a Python manifest counts as tracked only where an
entry for its ecosystem names its directory. A `uv.lock` implies `uv` and a `requirements*.txt`
implies `pip`, each for its own directory. A `pyproject.toml` declaring `[project]` dependencies
beside neither implies `pip` or `uv`. It implies nothing where it is a member of a uv workspace
whose root `uv.lock` is tracked, since that root's own entry covers it. A config-only
`pyproject.toml` implies nothing.
"""

import glob
import posixpath
import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

Finding = tuple[str, str]


def _updates(doc: Any) -> list[dict[str, Any]]:
    updates = doc.get("updates") if isinstance(doc, dict) else None
    return [u for u in updates if isinstance(u, dict)] if isinstance(updates, list) else []


def ecosystems(doc: Any) -> set[str]:
    """The package-ecosystem values a parsed dependabot.yml declares."""
    return {str(u["package-ecosystem"]) for u in _updates(doc) if u.get("package-ecosystem")}


def _directory(value: str) -> str:
    return value.strip("/") or "."


def declared_directories(doc: Any) -> dict[str, list[str]]:
    """Each ecosystem's directories as tree directory patterns, '.' for the root.

    Dependabot reads a glob in `directories` alone, so a `directory` value is escaped to match only itself.
    """
    out: dict[str, list[str]] = {}
    for update in _updates(doc):
        if not update.get("package-ecosystem"):
            continue
        patterns = out.setdefault(str(update["package-ecosystem"]), [])
        if update.get("directory"):
            patterns.append(glob.escape(_directory(update["directory"])))
        patterns.extend(_directory(v) for v in update.get("directories", []))
    return out


def manifest_ecosystem(path: str) -> str | None:
    """The ecosystem a `uv.lock` or `requirements*.txt` at `path` implies for its own directory, else None."""
    name = posixpath.basename(path)
    if name == "uv.lock":
        return "uv"
    return "pip" if name.startswith("requirements") and name.endswith(".txt") else None


def _table(doc: dict[str, Any], *keys: str) -> dict[str, Any]:
    """The nested TOML table `keys` name, or {} where any level is absent."""
    for key in keys:
        doc = doc.get(key, {})
    return doc


def _declares_dependencies(doc: dict[str, Any]) -> bool:
    project = _table(doc, "project")
    optional = project.get("optional-dependencies", {})
    return bool(project.get("dependencies")) or any(optional.values())


def _workspace_member(directory: str, root: str, workspace: dict[str, Any]) -> bool:
    """Whether `directory` matches a `members` glob of the workspace at `root` and no `exclude` glob."""
    if root == directory or not (root == "." or directory.startswith(root + "/")):
        return False
    relative = PurePosixPath(posixpath.relpath(directory, root))
    return any(relative.full_match(g) for g in workspace.get("members", [])) and not any(
        relative.full_match(g) for g in workspace.get("exclude", [])
    )


def python_needs(
    tree: Iterable[str], read: Callable[[str], str | None]
) -> tuple[dict[tuple[str, tuple[str, ...]], str], list[Finding]]:
    """`({(directory, ecosystems any one of which tracks it): why}, findings)` over the tree's manifests.

    `read` answers a tracked path's text, or None where it cannot. A `pyproject.toml` it cannot
    read or parse is a finding, since whether it declares dependencies is then unknown.
    """
    paths = sorted(tree)
    needs: dict[tuple[str, tuple[str, ...]], str] = {}
    for path in paths:
        ecosystem = manifest_ecosystem(path)
        if ecosystem:
            needs.setdefault((posixpath.dirname(path) or ".", (ecosystem,)), f"{path} is present")
    findings: list[Finding] = []
    projects: dict[str, dict[str, Any]] = {}
    for path in (p for p in paths if posixpath.basename(p) == "pyproject.toml"):
        text = read(path)
        try:
            doc = None if text is None else tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            doc = None
        if doc is None:
            findings.append(
                (
                    "DRIFT",
                    f"dependabot: {path} could not be read as TOML, so whether it declares [project] dependencies is unread; verify by hand",
                )
            )
        else:
            projects[posixpath.dirname(path) or "."] = doc
    locked = {d for d, ecos in needs if ecos == ("uv",)}
    workspaces = {
        root: _table(doc, "tool", "uv", "workspace")
        for root, doc in projects.items()
        if root in locked and _table(doc, "tool", "uv", "workspace")
    }
    claimed = {d for d, _ in needs}
    for directory, doc in projects.items():
        if directory in claimed or not _declares_dependencies(doc):
            continue
        if any(_workspace_member(directory, r, w) for r, w in workspaces.items()):
            continue
        where = "pyproject.toml" if directory == "." else f"{directory}/pyproject.toml"
        needs[(directory, ("pip", "uv"))] = (
            f"{where} declares [project] dependencies with no uv.lock or requirements*.txt"
        )
    return needs, findings


def python_findings(
    declared: Mapping[str, list[str]], tree: Iterable[str], read: Callable[[str], str | None]
) -> list[Finding]:
    """DRIFT for each Python directory no entry of an ecosystem it implies names."""
    needs, findings = python_needs(tree, read)
    for (directory, options), why in sorted(needs.items()):
        if any(
            PurePosixPath(directory).full_match(p) for e in options for p in declared.get(e, [])
        ):
            continue
        shown = "/" if directory == "." else f"/{directory}"
        findings.append(
            (
                "DRIFT",
                f"dependabot: {' or '.join(options)} ecosystem not declared for directory {shown} though {why}; add it for both main and develop per the fleet norm",
            )
        )
    return findings
