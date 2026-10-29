#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py>=4"]
# ///
"""Check or apply manifest-owned trees, and the verbatim sections inside a mixed file, from the hub to a fleet worktree."""

# The repository gate's `carried-trees` check loads this whole file under plain `python3`, 3.12 on a CI runner, and calls its tree functions.
# The file's top level and those functions therefore stay on the standard library and on 3.12.
# The gate reports a failure to load or run them as a note.

import argparse
import fnmatch
import functools
import hashlib
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The hub authors every canonical, so its own copies are the source a re-vendor reads rather than a target one writes.
HUB_NAME = "ProjectTemplate"


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
    if "\\" in value:
        raise CarryError(f"path must not carry a backslash: {value}")
    if any(pathlib.PureWindowsPath(part).anchor for part in declared.parts):
        raise CarryError(f"path must not carry a Windows drive or root in any component: {value}")
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


def target_patterns(declaration: dict[str, Any]) -> list[str]:
    """What a declaration owns under its target: everything where it prunes, its `include` where it does not."""
    return ["**/*"] if declaration.get("prune") else declaration["include"]


def compare_declaration(
    declaration: dict[str, Any], source: Inventory, target: Inventory | None
) -> dict[str, Any]:
    """`compare`, with the extras cleared where the declaration does not prune, since it owns only what it carries there."""
    result = compare(source, target)
    if not declaration.get("prune"):
        result["extra"] = []
        result["extraDirectories"] = []
    return result


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
            changes.append(f"remove {directory.relative_to(repository_root).as_posix()}")
            directory = directory.parent

    created_roots = []
    current = target_root
    while current != repository_root and not current.exists():
        created_roots.append(current)
        current = current.parent
    target_root.mkdir(parents=True, exist_ok=True)
    changes.extend(
        f"create {directory.relative_to(repository_root).as_posix()}"
        for directory in reversed(created_roots)
    )
    for relative in result["missingDirectories"]:
        destination = target_root / relative
        destination.mkdir(parents=True, exist_ok=True)
        changes.append(f"create {destination.relative_to(repository_root).as_posix()}")
    for relative in [*result["missing"], *result["modified"]]:
        destination = target_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.files[relative])
        changes.append(f"write {destination.relative_to(repository_root).as_posix()}")
    for relative in result["extra"]:
        destination = target_root / relative
        destination.unlink()
        changes.append(f"remove {destination.relative_to(repository_root).as_posix()}")
        remove_empty_ancestors(destination.parent)
    for relative in sorted(
        result["extraDirectories"],
        key=lambda value: len(pathlib.PurePosixPath(value).parts),
        reverse=True,
    ):
        directory = target_root / relative
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
            changes.append(f"remove {directory.relative_to(repository_root).as_posix()}")
            remove_empty_ancestors(directory.parent)
    return changes


def git(root: pathlib.Path, *args: str) -> str:
    # `rev-parse --show-toplevel` and `worktree list --porcelain` emit a path raw, without the quoting that keeps other git output ASCII.
    # A strict decode of one that is not UTF-8 raises here uncaught, ending the run in a traceback.
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if result.returncode != 0:
        raise CarryError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def git_is_ancestor(root: pathlib.Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode not in (0, 1):
        raise CarryError(result.stderr.strip() or "git merge-base --is-ancestor failed")
    return result.returncode == 0


def git_status_records(root: pathlib.Path, *, ignored: bool = False) -> list[tuple[str, str]]:
    """Each changed or untracked path, and each ignored one when asked, with its status code.

    A rename or copy yields its destination and then its source, each with the record's status.
    """
    command = ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if ignored:
        command.append("--ignored")
    result = subprocess.run(command, cwd=root, capture_output=True, check=False)
    if result.returncode != 0:
        raise CarryError(os.fsdecode(result.stderr).strip() or "git status failed")
    fields = result.stdout.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    records: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        if len(entry) < 4 or entry[2:3] != b" ":
            raise CarryError("git status returned malformed porcelain output")
        status = os.fsdecode(entry[:2])
        records.append((status, os.fsdecode(entry[3:])))
        if "R" in status or "C" in status:
            index += 1
            if index >= len(fields):
                raise CarryError("git status returned an incomplete rename or copy")
            records.append((status, os.fsdecode(fields[index])))
        index += 1
    return records


def git_status_paths(root: pathlib.Path) -> list[str]:
    return [path for _, path in git_status_records(root)]


def normalized_origin(value: str) -> str:
    value = value.strip().rstrip("/").removesuffix(".git")
    if value.startswith("git@github.com:"):
        return "https://github.com/" + value.removeprefix("git@github.com:")
    if value.startswith("ssh://git@github.com/"):
        return "https://github.com/" + value.removeprefix("ssh://git@github.com/")
    return value


def verify_hub(hub: pathlib.Path, registry: dict[str, Any]) -> str:
    entry = resolve_repo(HUB_NAME, registry)
    origin = normalized_origin(git(hub, "config", "--get", "remote.origin.url"))
    if origin != normalized_origin(entry["url"]):
        raise CarryError(f"hub origin does not match the {HUB_NAME} registry entry")
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


def validate_declarations(declarations: list[Any], hub: pathlib.Path) -> None:
    required = {"source", "target", "fidelity", "appliesTo", "include", "prune"}
    allowed = required | {"allowHubTarget"}
    for index, left in enumerate(declarations):
        if not isinstance(left, dict):
            raise CarryError(f"tree declaration must be an object: {left!r}")
        missing = required - set(left)
        unknown = set(left) - allowed
        if missing:
            raise CarryError(f"tree declaration is missing: {', '.join(sorted(missing))}")
        if unknown:
            raise CarryError(f"tree declaration has unknown fields: {', '.join(sorted(unknown))}")
        if not isinstance(left["source"], str) or not left["source"]:
            raise CarryError("tree declaration source must be a non-empty string")
        if not isinstance(left["target"], str) or not left["target"]:
            raise CarryError("tree declaration target must be a non-empty string")
        if left.get("fidelity") != "verbatim-tree":
            raise CarryError(f"tree declaration has unsupported fidelity: {left.get('fidelity')}")
        selector = left["appliesTo"]
        if not (
            isinstance(selector, str)
            or (
                isinstance(selector, list)
                and selector
                and all(isinstance(item, str) for item in selector)
            )
        ):
            raise CarryError(
                "tree declaration appliesTo must be a string or non-empty string array"
            )
        include = left["include"]
        if not (
            isinstance(include, list)
            and include
            and all(isinstance(pattern, str) and pattern for pattern in include)
        ):
            raise CarryError("tree declaration include must be a non-empty string array")
        if not isinstance(left["prune"], bool):
            raise CarryError("tree declaration prune must be a boolean")
        if "allowHubTarget" in left and not isinstance(left["allowHubTarget"], bool):
            raise CarryError("tree declaration allowHubTarget must be a boolean")
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
    git(target, "fetch", "origin", "develop")
    if not git_is_ancestor(target, "origin/develop", "HEAD"):
        raise CarryError("target branch must contain the current origin/develop head")
    worktree_rows = git(target, "worktree", "list", "--porcelain").splitlines()
    registered = [
        pathlib.Path(row.removeprefix("worktree ")).resolve()
        for row in worktree_rows
        if row.startswith("worktree ")
    ]
    if registered.count(target.resolve()) != 1:
        raise CarryError("target is not a registered git worktree")
    dirty = []
    for relative in git_status_paths(target):
        path = (target / relative).resolve(strict=False)
        if not any(path == root or root in path.parents for root in owned_roots):
            dirty.append(relative)
    if dirty:
        raise CarryError(f"target has unrelated changes: {', '.join(sorted(dirty))}")


@functools.cache
def fidelity_text_module() -> Any:
    """`spec/fidelity_text.py`, imported on first use rather than at module import, then held.

    It is a script-directory module rather than a package one, so the spec directory has to be
    importable before it is. Lazily, because it imports markdown-it-py and `scripts/handoff.py`
    imports this module under a plain `python3` for `normalize_eol` alone. A tree carry never
    reads a region either.
    """
    spec = str(ROOT / "spec")
    if spec not in sys.path:
        sys.path.insert(0, spec)
    import fidelity_text

    return fidelity_text


# A line and its own terminator, splitting on LF alone. `str.splitlines` also breaks on a form feed and on several Unicode separators.
# Those are content in a carried document rather than line boundaries.
# So splitting on it would move bytes this tool was asked to leave alone.
_LINE = re.compile(r"[^\n]*\n|[^\n]+$")


def split_lines(text: str, rel: str | None = None) -> list[str]:
    """`text` as lines that keep their own terminators, so joining them returns the original bytes.

    A lone CR is refused rather than read as content.
    That is because `spec/fidelity_text.py`'s `normalize` treats one as a line break and this does not.
    The two would therefore read a CR-terminated document as different line sets.
    The audit would report a section stale that this tool cannot even locate.
    The refusal it would otherwise raise names an absent section rather than the real problem.
    """
    if rel is not None and "\r" in text.replace("\r\n", ""):
        raise CarryError(
            f"{rel} carries a bare carriage return, which the fidelity comparison reads as a line"
            " break and this tool does not, so the two would disagree about where a section stops"
        )
    return _LINE.findall(text)


def terminator(line: str) -> str:
    """The line's own terminator, or the empty string for a final line that ends the file without one."""
    if line.endswith("\r\n"):
        return "\r\n"
    return "\n" if line.endswith("\n") else ""


def file_ending(lines: list[str]) -> str:
    """The terminator this file uses, read from its first terminated line.

    A replacement takes its ending from the section's own heading line.
    That line carries none when the heading is the last line of a file with no final newline.
    Falling back to LF there would write LF into a CRLF file and leave it mixed.
    So the file answers for itself, and LF is the answer only for a file that has no terminated line at all.
    """
    for line in lines:
        if terminator(line):
            return terminator(line)
    return "\n"


def normalize_eol(text: str) -> str:
    """`text` with every line ending as LF.

    Deliberately not `spec/fidelity_text.py`'s `normalize`, which also masks a Dependabot-owned action pin
    and a per-repository `needs:` list. Those are governed drift for a comparison asking whether
    two copies say the same thing. This tool writes bytes, so masking either would let it
    overwrite a value the repository owns. Line endings are the one exception, because
    `.gitattributes` governs them separately and a re-vendor preserves whichever the target uses.
    `scripts/canonical_review.py`'s own `normalize` is this same reduction for the same reason.
    It is not shared, since that module imports this one and importing it back would be a cycle.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def h2_headings(lines: list[str]) -> list[str]:
    """Every level-two heading's parsed text, in order, reading a `## ` inside a code block as content."""
    return [h.text for h in fidelity_text_module().headings("".join(lines)) if h.level == 2]


def section_span(lines: list[str], heading: str) -> tuple[int, int] | None:
    """The `[start, end)` line range of the `## <heading>` region, or None when the file has none.

    Boundaries are `spec/fidelity_text.py`'s, which the audit's `extract_section` also uses.
    So a `## ` shown inside a code sample ends a region for neither tool. The heading line is inside
    the region, and a sibling level-two heading ends it. The match is on the parsed heading text
    case-folded, so a re-cased heading is found rather than read as absent. The range is positional
    rather than the text itself. That lets the caller replace the region without also replacing an
    identical passage quoted elsewhere in the document.
    """
    span = fidelity_text_module().section_span("".join(lines), heading)
    if span is None:
        return None
    return span[0], len(lines) if span[1] is None else span[1]


def comparable_region(lines: list[str], span: tuple[int, int]) -> str:
    """The region rendered the way `spec/fidelity_text.py`'s `extract_section` renders it.

    That function splits the whole document on LF and joins the region's lines back with LF.
    So a file ending in a newline yields one final empty element, and a region running to the end
    of the file carries it. The consequence is worth stating, because it decides what this tool
    writes. The following render to the identical string:

    - a section that ends the hub's file
    - the same section followed by a heading downstream, with one blank line between

    The fidelity
    comparison therefore reads them as the same content, and the raw bytes do not. So this is the
    form to compare. It is not the form to write. `replace_sections` builds its own lines, because
    writing needs the target's own terminators and this form has flattened them.
    """
    bodies = [line[: len(line) - len(terminator(line))] for line in lines[span[0] : span[1]]]
    if span[1] == len(lines) and lines and terminator(lines[-1]):
        bodies.append("")
    return "\n".join(bodies)


def outside_sections(lines: list[str], sections: list[str], path: str) -> str:
    """Every line outside the named regions, joined, which is the text a re-vendor must not touch.

    Includes the preamble, every intent section, and every verbatim section this run left alone.
    So comparing it before and after is what proves the write stayed inside the regions it named.
    """
    covered: set[int] = set()
    for section in sections:
        span = section_span(lines, section)
        if span is None:
            raise CarryError(
                f"{path} section '{section}' is absent, so the untouched text cannot be compared"
            )
        covered.update(range(*span))
    return "".join(line for index, line in enumerate(lines) if index not in covered)


def guard_governed_drift(path: str, section: str, region: str, side: str) -> None:
    """Refuse a section carrying content the fidelity comparison normalizes away.

    A `uses: <action>@<sha>` pin with its `# vN` comment, and a job's `needs:` list, are owned per
    repository (`spec/fidelity-model.md` "Normalization"). So writing the hub's bytes over either
    would revert a bump or a prune the repository owns, and the audit would report nothing. No
    section declared verbatim carries either today. This refuses rather than discovering it
    silently the first time one does.
    """
    if fidelity_text_module().normalize(region) != normalize_eol(region):
        raise CarryError(
            f"{path} section '{section}' carries content the fidelity comparison normalizes away"
            f" ({side} copy), so re-vendoring its bytes could revert per-repository drift."
            " Re-vendor that section by hand"
        )


def verbatim_section_names(item: dict[str, Any], selectors: set[str]) -> list[str]:
    """The section names this repository carries at verbatim fidelity, in manifest order.

    A bare-string entry is `appliesTo` `*` at intent fidelity, matching `spec/audit.py`'s own
    reading of the same manifest, so it is never a re-vendor target.
    """
    out: list[str] = []
    for element in item.get("sections", []):
        if isinstance(element, str):
            continue
        if not isinstance(element, dict):
            raise CarryError(f"section declaration must be a string or an object: {element!r}")
        if element.get("fidelity") != "verbatim":
            continue
        if not applicable(element.get("appliesTo", "*"), selectors):
            continue
        name = element.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CarryError("section declaration name must be a non-empty string")
        # Folded and stripped, matching `section_span`'s own lookup key.
        # That is because two spellings of one heading resolve to one region, and replacing that region twice would splice its tail in again.
        if name.strip().lower() not in {seen.strip().lower() for seen in out}:
            out.append(name)
    return out


def section_units(manifest: dict[str, Any], selectors: set[str]) -> list[tuple[str, list[str]]]:
    """`(path, [section])` for every baseline file this repository carries verbatim sections of.

    One path declared by more than one baseline entry merges into one unit, since the file is written once.
    The manifest declares a path in more than one entry for a file whose applicability differs by repository type.
    """
    baseline = manifest.get("baseline")
    if not isinstance(baseline, list):
        raise CarryError("manifest baseline must be an array")
    units: dict[str, list[str]] = {}
    placeholder_paths = set()
    for item in baseline:
        if not isinstance(item, dict):
            raise CarryError(f"baseline declaration must be an object: {item!r}")
        if not applicable(item.get("appliesTo", "*"), selectors):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise CarryError("baseline declaration path must be a non-empty string")
        # Collected for every applicable entry rather than only the ones carrying verbatim sections.
        # That is because the two can be declared on separate entries for one path, and a file is written once whichever entry named the substitution.
        if item.get("placeholders"):
            placeholder_paths.add(path)
        names = verbatim_section_names(item, selectors)
        if not names:
            continue
        if not path.endswith(".md"):
            raise CarryError(
                f"a verbatim section is a Markdown heading region, so it cannot be declared on {path}"
            )
        merged = units.setdefault(path, [])
        seen = {name.strip().lower() for name in merged}
        merged.extend(name for name in names if name.strip().lower() not in seen)
    for path in sorted(set(units) & placeholder_paths):
        raise CarryError(
            f"{path} declares both placeholders and verbatim sections, and a verbatim unit carries"
            " no placeholder (spec/fidelity-model.md 'Normalization'), so re-vendoring one could"
            " overwrite a substituted value"
        )
    return list(units.items())


def replace_sections(
    target_lines: list[str], source_lines: list[str], sections: list[str], path: str
) -> list[str]:
    """`target_lines` with each named region replaced by the hub's, every other line untouched.

    Spliced from the last region to the first, so an earlier replacement cannot move a later
    region's indices. Each replacement carries the terminator the section's own heading line
    carried. That is how a CRLF target keeps its endings without the file being rewritten to one
    ending it may not use throughout.
    """
    spans = []
    for section in sections:
        target_span = section_span(target_lines, section)
        source_span = section_span(source_lines, section)
        if target_span is None or source_span is None:
            raise CarryError(f"{path} section '{section}' could not be located to replace")
        spans.append((target_span, source_span))
    ordered = sorted(spans, reverse=True)
    # Two names resolving to one region would splice that region's replacement in twice, the second time against indices the first splice already moved.
    # The file would then gain a duplicated tail before any postcondition read it back.
    for (later, _), (earlier, _) in itertools.pairwise(ordered):
        if earlier[1] > later[0]:
            raise CarryError(
                f"{path} names two regions that overlap, lines {earlier} and {later},"
                " so one replacement would splice over the other"
            )
    out = list(target_lines)
    default_ending = file_ending(target_lines)
    for (start, end), source_span in ordered:
        ending = terminator(out[start]) or default_ending
        region = []
        for line in source_lines[source_span[0] : source_span[1]]:
            body = line[: len(line) - len(terminator(line))]
            region.append(body + ending if terminator(line) else body)
        # A region spliced ahead of more of the file has to end terminated, or its last line would run into the heading that follows it.
        if region and not terminator(region[-1]):
            region[-1] += ending
        # A region runs to the next level-two heading.
        # So the blank line before that heading is inside it, and a section ending its file has none.
        # Where the two copies differ on that, `comparable_region` reads both as the same content.
        # So the boundary is reconciled to what this document needs rather than copied from the other one.
        # The reconciliation pads where a heading follows, and drops the trailing blank where nothing does.
        source_ends_file = source_span[1] == len(source_lines)
        target_ends_file = end == len(out)
        if source_ends_file and not target_ends_file:
            region.append(ending)
        elif target_ends_file and not source_ends_file and region and not region[-1].strip():
            region.pop()
        out[start:end] = region
    return out


def assert_sections(
    path: str,
    target_file: pathlib.Path,
    source_lines: list[str],
    replaced: list[str],
    declared: list[str],
    before_lines: list[str],
) -> None:
    """Re-read the written file and prove the three things the re-vendor promised.

    Stated as an assertion the tool makes about its own output rather than as care taken while
    writing it. The defect this exists for is a single blank line dropped between the preamble and
    the first heading. It is invisible in a review of the diff and breaks no renderer, and no linter in
    the gate set flags it. So it would ship, and the next audit would report drift nobody could
    see. `apply` already re-compares tree digests after writing, and this is that discipline for a
    section.
    """
    after_lines = split_lines(decode_text(target_file, path), path)
    # Folded, because `section_span` locates a heading folded, so a re-cased heading is drift this tool fixes rather than a change of section set.
    # Any other edit to a heading line is caught byte-for-byte by the two comparisons below.
    if [name.lower() for name in h2_headings(after_lines)] != [
        name.lower() for name in h2_headings(before_lines)
    ]:
        raise CarryError(
            f"post-apply check failed for {path}: the level-two heading sequence changed,"
            " so the section set or its order did not survive the write"
        )
    if outside_sections(after_lines, replaced, path) != outside_sections(
        before_lines, replaced, path
    ):
        raise CarryError(
            f"post-apply check failed for {path}: text outside the re-vendored sections changed,"
            " so the preamble, an intent section, or a section this run left alone was modified"
        )
    for section in declared:
        after_span = section_span(after_lines, section)
        source_span = section_span(source_lines, section)
        if after_span is None or source_span is None:
            raise CarryError(f"post-apply check failed for {path}: section '{section}' is absent")
        if comparable_region(after_lines, after_span) != comparable_region(
            source_lines, source_span
        ):
            raise CarryError(
                f"post-apply check failed for {path} section '{section}':"
                " it still does not match the hub's canonical"
            )


def entry_types(entry: dict[str, Any]) -> list[str]:
    """The string elements of the entry's `types`, since `resolve_repo` returns an entry of any status and only a cataloged one has its shape validated."""
    types = entry.get("types")
    return [t for t in types if isinstance(t, str)] if isinstance(types, list) else []


def selector_set(entry: dict[str, Any], defaults: dict[str, Any]) -> set[str]:
    """The repository's applicability selectors: its types, workflow model, release trigger, and consumer model."""
    selectors = set(entry_types(entry))
    selectors.add(entry.get("workflowModel") or defaults.get("workflowModel") or "release")
    selectors.add(entry.get("releaseTrigger") or defaults.get("releaseTrigger") or "two-phase")
    if entry.get("consumerModel"):
        selectors.add(entry["consumerModel"])
    return selectors


def decode_text(path: pathlib.Path, rel: str) -> str:
    """A carried file's text, refusing rather than guessing at bytes that are not UTF-8."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CarryError(f"cannot read {rel}: {exc}") from exc


@dataclass(frozen=True)
class SectionPlan:
    """One file's re-vendor, decided but not yet written."""

    path: str
    declared: list[str]
    source_lines: list[str]
    target_lines: list[str]
    stale: list[str]


def plan_unit(
    hub: pathlib.Path, target: pathlib.Path, path: str, declared: list[str]
) -> SectionPlan:
    """Locate every declared section in both copies and decide which are stale, writing nothing."""
    source_file = relative_root(hub, path)
    target_file = relative_root(target, path)
    if not target_file.is_file():
        raise CarryError(
            f"{path} is absent from the target, which is a standup question rather than drift"
            " (the hub's STANDUP.md carries the baseline), so this refuses rather than creating it"
        )
    source_lines = split_lines(decode_text(source_file, path), path)
    target_lines = split_lines(decode_text(target_file, path), path)
    stale = []
    for section in declared:
        source_span = section_span(source_lines, section)
        if source_span is None:
            raise CarryError(
                f"{path} section '{section}' is declared verbatim but absent from the hub's own copy"
            )
        target_span = section_span(target_lines, section)
        if target_span is None:
            raise CarryError(
                f"{path} section '{section}' is declared verbatim but absent from the target."
                " Where a declared section belongs in a file that never carried it is a standup"
                " question rather than a drift question, so this refuses rather than guessing"
            )
        source_region = comparable_region(source_lines, source_span)
        target_region = comparable_region(target_lines, target_span)
        guard_governed_drift(path, section, source_region, "hub")
        guard_governed_drift(path, section, target_region, "target")
        if source_region != target_region:
            stale.append(section)
    return SectionPlan(path, declared, source_lines, target_lines, stale)


def plan_sections(
    hub: pathlib.Path, target: pathlib.Path, units: list[tuple[str, list[str]]]
) -> list[SectionPlan]:
    """Every unit's plan, so each refusal is raised before the first file is written.

    A refusal reached partway through the writes would leave one file re-vendored and the next
    not. It would be reported by the same exit code as a refusal that touched nothing.
    The operator would have no way to tell the two apart from the output.
    """
    return [plan_unit(hub, target, path, declared) for path, declared in units]


def apply_plans(target: pathlib.Path, plans: list[SectionPlan]) -> None:
    """Write each plan's stale regions and assert the result, one file at a time.

    Every refusal has already been raised by `plan_sections`, so nothing here can decline to
    proceed. What can still fail is the write itself or the assertion after it. Each names the
    file it was working on. That is because by then an earlier file may already hold its
    re-vendored content, and the operator has to know which.
    """
    for plan in plans:
        if not plan.stale:
            continue
        target_file = relative_root(target, plan.path)
        rewritten = replace_sections(plan.target_lines, plan.source_lines, plan.stale, plan.path)
        try:
            target_file.write_bytes("".join(rewritten).encode("utf-8"))
        except OSError as exc:
            raise CarryError(f"cannot write {plan.path}: {exc}") from exc
        print(f"write {plan.path}")
        assert_sections(
            plan.path,
            target_file,
            plan.source_lines,
            plan.stale,
            plan.declared,
            plan.target_lines,
        )
        print(json.dumps({"path": plan.path, "postApply": True, "stale": []}, sort_keys=True))


def run_sections(mode: str, name: str, target: pathlib.Path, hub: pathlib.Path = ROOT) -> int:
    """Compare, and for `apply-sections` re-vendor, the verbatim sections inside a mixed file.

    The gap between the two things that already exist:

    - `carry.py`'s tree modes own a whole tree.
    - The audit names a stale section precisely without writing anything.

    A file such as `AGENTS.md` is neither, holding fleet law in some sections and the repository's
    own content in others. Re-vendoring the whole file over it is the overwrite the
    `carried-instruction-file-guard` skill exists to stop.
    """
    # Ahead of every other check, because which repository was named does not depend on the hub's state.
    # And because reporting a stale hub for an argument that could never be valid names the wrong problem.
    if name == HUB_NAME:
        raise CarryError("the hub's own copy is the canonical, so it is never a re-vendor target")
    registry = load_json(hub / "registry/repos.json")
    manifest = load_json(hub / "spec/files.json")
    hub_commit = verify_hub(hub, registry)
    entry = resolve_repo(name, registry)
    selectors = selector_set(entry, registry.get("defaults", {}))
    units = section_units(manifest, selectors)
    # No owned root, unlike a tree carry.
    # A tree's root holds nothing but hub-owned content, so exempting it from the unrelated-changes check gives up nothing.
    # In contrast, these files hold the repository's own sections too.
    # Exempting one would let a write land on top of an uncommitted edit to a section the tool never touches.
    # The cost is that a second apply needs the first one committed.
    # RESYNC.md's step 1 runs these modes before anything else writes to the tree.
    verify_target(target, entry, [])
    plans = plan_sections(hub, target, units)
    print(f"hubCommit: {hub_commit}")
    print(f"repository: {name}")
    print(f"types: {','.join(entry_types(entry))}")
    print(f"files: {len(plans)}")
    for plan in plans:
        print(
            json.dumps(
                {"path": plan.path, "sections": plan.declared, "stale": plan.stale}, sort_keys=True
            )
        )
    if mode != "apply-sections":
        return 0 if all(not plan.stale for plan in plans) else 1
    apply_plans(target, plans)
    return 0


def refuse_hub_tree_target(name: str, declarations: list[dict[str, Any]]) -> None:
    """Refuse the hub as a tree-carry target unless every applicable declaration opts in.

    A tree declaration owns its target root outright and prunes what it does not carry.
    So pointing one at the hub aims a repository's own source tree at a copy of itself.
    `allowHubTarget` is the per-declaration opt in, and its absence is the refusal.
    Lifted out of `run` so it can be exercised without a hub checkout.
    `verify_hub` would otherwise demand one first.
    """
    if name == HUB_NAME and any(not item.get("allowHubTarget", False) for item in declarations):
        raise CarryError(f"a declaration does not allow {HUB_NAME} as its target")


def run(mode: str, name: str, target: pathlib.Path, hub: pathlib.Path = ROOT) -> int:
    registry = load_json(hub / "registry/repos.json")
    manifest = load_json(hub / "spec/files.json")
    hub_commit = verify_hub(hub, registry)
    entry = resolve_repo(name, registry)
    selectors = selector_set(entry, registry.get("defaults", {}))
    all_declarations = manifest.get("trees")
    if not isinstance(all_declarations, list):
        raise CarryError("manifest trees must be an array")
    validate_declarations(all_declarations, hub)
    declarations = [
        declaration
        for declaration in all_declarations
        if applicable(declaration.get("appliesTo", "*"), selectors)
    ]
    refuse_hub_tree_target(name, declarations)
    owned_roots = [relative_root(target, item["target"]) for item in declarations]
    verify_target(target, entry, owned_roots)
    print(f"hubCommit: {hub_commit}")
    print(f"repository: {name}")
    print(f"types: {','.join(entry_types(entry))}")
    print(f"declarations: {len(declarations)}")
    clean = True
    for declaration in declarations:
        source_root = relative_root(hub, declaration["source"])
        target_root = relative_root(target, declaration["target"])
        source = inventory(source_root, declaration["include"])
        target_inventory = (
            inventory(target_root, target_patterns(declaration)) if target_root.exists() else None
        )
        result = compare_declaration(declaration, source, target_inventory)
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
            final = compare_declaration(
                declaration, source, inventory(target_root, target_patterns(declaration))
            )
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
    parser.add_argument("mode", choices=("check", "apply", "check-sections", "apply-sections"))
    parser.add_argument("repository")
    parser.add_argument("--target", required=True, type=pathlib.Path)
    args = parser.parse_args()
    # The section modes are their own vocabulary rather than a scope flag on the tree modes.
    # So an existing invocation keeps meaning exactly what it did, and a caller names which of the two it wants.
    dispatch = run_sections if args.mode.endswith("-sections") else run
    try:
        return dispatch(args.mode, args.repository, args.target.resolve())
    except CarryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
