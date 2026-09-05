#!/usr/bin/env python3
"""Generate the GitHub and Claude-compatible copies of .agents/skills/.

.agents/skills/ is the one hand-authored source: Codex and opencode read it directly with no
install step. Claude Code never scans that path, only .claude/skills/ or a plugin's own skills/
directory, so this script materializes a plugin (.claude-plugin/fleet-skills/) that
.claude-plugin/marketplace.json publishes. GitHub Copilot discovers repository skills under
.github/skills/, so the script also materializes that tree. .agents/skills/ stays the single
place a skill is ever hand-edited, its include regions excepted, since those are generated from
the files they name.

A skill reads whole in isolation and a rule has one home, so the text a skill needs from that
home is generated into it rather than copied: a region between `<!-- include: <path> > <heading> -->`
and `<!-- /include -->` is filled with the body under that heading, in .agents/skills/ itself, and
--check fails when a region differs from what its source renders now. The key is the root-relative
path, then ` > `, then the heading text, the delimiter canonical_review.py also keys a unit on.

Usage: python3 scripts/build_dist.py           fill include regions, then regenerate distributions
       python3 scripts/build_dist.py --check   read-only: exit 0 clean, 1 stale, 2 on a real
                                                 failure (a symlink under .agents/skills/, an
                                                 unreadable file, an include region it cannot
                                                 render), so a caller reading the exit
                                                 code can tell a finding apart from the check
                                                 itself not having run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
SKILLS_SRC = ROOT / ".agents" / "skills"
PLUGIN_NAME = "fleet-skills"
DIST_PLUGIN = ROOT / ".claude-plugin" / PLUGIN_NAME
PLUGIN_MANIFEST = DIST_PLUGIN / ".claude-plugin" / "plugin.json"
# One digest file per skill rather than one stamp over every skill's bytes, so two branches editing two skills touch two files and merge (ptr727/ProjectTemplate#1240).
# Under the plugin root and not under .github/skills/, which spec/files.json carries whole to every fleet repository.
DIGEST_DIR = DIST_PLUGIN / ".source-digests"
GITHUB_SKILLS = ROOT / ".github" / "skills"


def skill_names():
    """Every .agents/skills/<name>/ directory that carries a SKILL.md, sorted for a stable digest."""
    if not SKILLS_SRC.is_dir():
        return []
    return sorted(p.name for p in SKILLS_SRC.iterdir() if (p / "SKILL.md").is_file())


def reject_symlinks(skill_dir):
    """Raise if `skill_dir` itself, or anything in its tree, is a symlink.

    .agents/skills/ is the one hand-authored source, with no legitimate reason to hold a symlink.
    Path.is_file() and read_bytes() both dereference one, and so does shutil.copytree() by
    default, so an unnoticed symlink there would let the digest and the generated plugin silently
    include content from outside this tree, tracked or not. `skill_dir` itself needs its own
    check: rglob("*") only yields paths *inside* the directory it walks, so a skill directory that
    is itself a symlink to another tree would walk straight into that tree without the walk ever
    seeing (or flagging) the root symlink node.
    """
    if skill_dir.is_symlink():
        raise ValueError(f"{skill_dir} is a symlink; .agents/skills/ must contain only real files")
    for p in skill_dir.rglob("*"):
        if p.is_symlink():
            raise ValueError(f"{p} is a symlink; .agents/skills/ must contain only real files")


def tree_digest(root, names):
    """One digest over every file under `root/<name>` for each name, order-independent per skill.

    Rooted at an arbitrary directory rather than hardcoding SKILLS_SRC, so the same function
    hashes both the source tree and the generated tree, and is_stale() can compare the two
    directly instead of trusting a stored digest to still describe what was actually generated.

    Names are hashed in the fixed sorted order the caller already produced. Each skill's own files
    are hashed in a second stable sort so an unrelated filesystem listing order never changes the
    digest on a machine where nothing changed.
    """
    h = hashlib.sha256()
    for name in names:
        skill_dir = root / name
        reject_symlinks(skill_dir)
        h.update(name.encode("utf-8"))
        # Sorted by the same .as_posix() key that gets hashed, not by raw Path comparison.
        # Path ordering follows the platform's native separator.
        # Two OSes can sort an identical file set into a different order and hash it into a different digest, even though the bytes hashed per file already agree via .as_posix() below.
        files = sorted(
            (f for f in skill_dir.rglob("*") if f.is_file()),
            key=lambda f: f.relative_to(root).as_posix(),
        )
        for f in files:
            h.update(f.relative_to(root).as_posix().encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def skill_digest(name):
    """The digest of one skill's authored files, which is what its stamp under DIGEST_DIR holds."""
    return tree_digest(SKILLS_SRC, [name])


def has_exact_entries(root, names, directories):
    """Whether `root` exists and holds exactly one entry per name, each a directory or each a file."""
    if not root.is_dir() or root.is_symlink():
        return False
    entries = list(root.iterdir())
    # A symlink satisfies is_dir() and is_file(), which follow it, so an entry pointing outside the tree would otherwise pass as generated content.
    shaped = all(
        not entry.is_symlink() and (entry.is_dir() if directories else entry.is_file())
        for entry in entries
    )
    return shaped and {entry.name for entry in entries} == set(names)


def has_exact_skill_directories(root, names):
    """Whether `root` exists and contains only the expected skill directories."""
    return has_exact_entries(root, names, directories=True)


def expected_manifest(names):
    """The plugin.json content `names` should produce, entirely deterministic.

    The one source is_stale() compares the actual manifest against, so a hand-edited field of
    any kind (not only "skills") is caught the same way a hand-edited "skills" list already was.
    """
    return {
        "name": PLUGIN_NAME,
        "version": "0.0.0",
        "description": "Fleet-wide agent rules and per-language conventions packaged as Claude "
        "Code Skills, generated from .agents/skills/.",
        "author": {"name": "ptr727"},
        "skills": [f"./skills/{name}" for name in names],
    }


def write_plugin_manifest(names):
    PLUGIN_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    manifest = expected_manifest(names)
    # LF, matching this repo's JSON default (.editorconfig `[*] end_of_line = lf`).
    # Explicit, not the platform default: a Windows host writing plain LF (newline=None) would translate it to CRLF on write, which disagrees with this repo's LF default.
    PLUGIN_MANIFEST.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


# --- Include regions -------------------------------------------------------------------------
# A skill has to read whole in isolation, and a rule has one home, so the text a skill needs from that home is generated into it rather than copied.
# The region is filled in the authored tree itself, because Codex and opencode read .agents/skills/ directly and a region left empty there would hand them a skill with a hole in it.
# The generated trees then mirror the filled source.

# The same `<path> > <heading>` vocabulary canonical_review.py keys a unit on, defined here because that engine imports this module.
SECTION_DELIM = " > "
# Sources resolve against the repository root, so a key reads the same in a skill, a finding, and the review ledger.
INCLUDE_ROOT = ROOT
_INCLUDE_START = re.compile(r"^<!--\s*include:\s*(?P<key>\S.*?)\s*-->$")
_INCLUDE_END = re.compile(r"^<!--\s*/include\s*-->$")
# Every level, so a level-one heading ends a body, though a key names level two down, since a level-one heading is a document's title rather than a section a skill would carry.
_HEADING = re.compile(r"^(#{1,6})\s+(?P<text>\S.*?)\s*$")
# CommonMark's own bound for a fence, so a marker sitting in an indented code block is content here the way it is there.
# A heading is matched at any indentation instead, the way spec/audit.py and canonical_review.py read one, so the three tools split a document alike.
_MAX_INDENT = 3


def _fence_step(line, marker, marker_len):
    """spec/audit.py's fence reading, imported on first use rather than at module import.

    One reading of CommonMark across the fidelity checks, the review ledger, and this generator,
    rather than a second one here that could disagree with them. Imported lazily because
    scripts/skills_install.py imports this module on hosts whose Python predates what audit.py
    needs, and installing never fills a region.
    """
    spec = str(ROOT / "spec")
    if spec not in sys.path:
        sys.path.insert(0, spec)
    import audit

    return audit._fence_step(line, marker, marker_len)


def _lf(text):
    """`text` with every line ending as LF, so a scan sees one line shape whatever the file carries."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_lines(path):
    """`(lines, newline, mixed)` for a text file, with the newline the file uses kept for writing it back.

    Detected from the bytes rather than assumed, because a generated rewrite that flattened a CRLF
    file to LF would be the line-ending corruption GOVERNANCE.md "Verification Discipline" names.
    A file mixing the two cannot be written back as it was, so `mixed` lets a caller refuse to
    render into one rather than rewrite it to one ending. UnicodeDecodeError is a ValueError,
    which is the exit-2 path a caller already reads as the check itself not having run.
    """
    data = path.read_bytes()
    crlf = data.count(b"\r\n")
    lone_cr = data.count(b"\r") - crlf
    lone_lf = data.count(b"\n") - crlf
    mixed = sum(1 for count in (crlf, lone_cr, lone_lf) if count) > 1
    newline = "\r\n" if crlf else "\r" if lone_cr else "\n"
    return _lf(data.decode("utf-8")).split("\n"), newline, mixed


def _unindented(line):
    """The line's text when it sits within CommonMark's three-space bound, else None, since deeper is an indented code block."""
    stripped = line.lstrip(" ")
    return stripped.rstrip() if len(line) - len(stripped) <= _MAX_INDENT else None


def _marker(line, marker, marker_len):
    """One line's marker match under fence state: `(state, start_match, end_match)`, both None inside a fence.

    A marker shown inside a code sample, which is how the skill-lifecycle skill documents the
    syntax, is content rather than a region boundary, whether the sample is fenced or indented.
    """
    marker, marker_len, boundary = _fence_step(line, marker, marker_len)
    text = None if boundary or marker is not None else _unindented(line)
    if text is None:
        return (marker, marker_len), None, None
    return (marker, marker_len), _INCLUDE_START.match(text), _INCLUDE_END.match(text)


def _exact_case(rel, parts):
    """Refuse a key whose spelling differs from the tree's, so a key resolves alike on every host.

    A case-insensitive filesystem would resolve `agents.md` to `AGENTS.md`, so a region keyed that
    way would fill on a macOS or Windows host and exit 2 on Linux CI.
    """
    current = INCLUDE_ROOT
    for part in parts:
        if part not in os.listdir(current):
            raise ValueError(f"include source {rel!r} is not spelled as the tree spells it")
        current = current / part


def include_source(rel):
    """The file an include key names, refused where it could name anything but authored text under the root.

    A generated tree is never a source, because its text is this script's own output and a region
    filled from one would round-trip through the mirror it exists to keep honest. A path that
    resolves to anything but itself went through a symlink, and one on the way could reach outside
    the root or into a generated tree under a name that looks fine lexically, so none is followed.
    """
    parts = PurePosixPath(rel)
    if not rel or parts.is_absolute() or ".." in parts.parts or parts.as_posix() != rel:
        raise ValueError(
            f"include source {rel!r} is not a plain path relative to the repository root"
        )
    path = INCLUDE_ROOT / parts
    if not path.is_file():
        raise ValueError(f"include source {rel!r} is not a file under the repository root")
    _exact_case(rel, parts.parts)
    resolved = path.resolve()
    if resolved != INCLUDE_ROOT.resolve() / parts:
        raise ValueError(f"include source {rel!r} is reached through a symlink")
    for generated in (DIST_PLUGIN, GITHUB_SKILLS):
        target = generated.resolve()
        if resolved == target or target in resolved.parents:
            raise ValueError(
                f"include source {rel!r} is under a generated tree, which is never a source"
            )
    return path


def heading_body(lines, heading, key):
    """The lines under the one heading whose text case-folds to `heading`, up to the next heading at its level or above.

    Any level from two down, so a key can name a GOVERNANCE.md section, an AGENTS.md subsection, or
    a section of a sibling skill with one vocabulary. Matched the way spec/audit.py matches a
    declared section, on the parsed text case-folded and at any indentation, so a re-cased heading
    still resolves and the two split a document alike. Two matches are two answers to one
    question, refused rather than resolved to the first, and a heading inside a fenced code block
    is content.
    """
    want = heading.strip().lower()
    headings = []
    state = (None, 0)
    for index, line in enumerate(lines):
        state_marker, state_len, boundary = _fence_step(line, *state)
        state = (state_marker, state_len)
        if boundary or state_marker is not None:
            continue
        m = _HEADING.match(line.strip())
        if m:
            headings.append((index, len(m.group(1)), m.group("text").strip().lower()))
    matches = [(index, depth) for index, depth, text in headings if text == want and depth > 1]
    if not matches:
        raise ValueError(f"include {key!r}: no heading {heading.strip()!r} in its source")
    if len(matches) > 1:
        raise ValueError(
            f"include {key!r}: {len(matches)} headings match, so the region cannot choose one"
        )
    start, depth = matches[0]
    end = next(
        (index for index, level, _ in headings if index > start and level <= depth), len(lines)
    )
    # The source's own region markers belong to its regions, not to the text they enclose.
    # A copied marker would open a region inside the one being filled on the next scan.
    # Dropping a marker leaves the blank line the fill put on each side of it beside the source's own, and two blanks in a row are what markdownlint refuses in a file nobody may hand-edit.
    # Inside a fence a doubled blank is the sample's own text, so it stays.
    # An indented code block has no closing line to track, so the last text line's indentation stands in for its state, erring toward keeping a blank.
    body = []
    state = (None, 0)
    indented = False
    for line in lines[start + 1 : end]:
        state, opens, closes = _marker(line, *state)
        blank = not line.strip()
        if not blank:
            indented = _unindented(line) is None
        doubled = state[0] is None and not indented and blank and body and not body[-1].strip()
        if opens is None and closes is None and not doubled:
            body.append(line)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if state[0] is not None:
        # A fence the body leaves open would swallow the end marker on the next scan and blame the skill for a defect in its source.
        raise ValueError(f"include {key!r}: the heading's body leaves a code fence open")
    if not body:
        # Rendering nothing would leave two blank lines between the markers, which markdownlint refuses in a file nobody may hand-edit.
        raise ValueError(
            f"include {key!r}: the heading's body is empty, so there is nothing to include"
        )
    return body


def include_body(key, stack):
    """What the region for `key` holds: the named heading's body, taken from the source as it renders now."""
    rel, delimiter, heading = key.partition(SECTION_DELIM)
    if not delimiter or not heading.strip():
        raise ValueError(f"include key {key!r} is not '<path>{SECTION_DELIM}<heading>'")
    lines, _, _ = filled_lines(rel, stack)
    return heading_body(lines, heading, key)


def filled_lines(rel, stack=()):
    """`(lines, newline, has_regions)` for `rel` with every include region holding its source's current content.

    Recursive, so a region filled from a file that carries regions of its own reads the text that
    file renders rather than its markers. A cycle is refused by the path that would loop, compared
    as files rather than as spellings, so an alias through a directory symlink cannot slip past.
    A file with no region comes back exactly as read, so filling it never rewrites anything,
    though it is still decoded to be scanned, so one that is not UTF-8 is refused either way.
    """
    path = include_source(rel)
    if any(path.samefile(seen) for seen in stack):
        raise ValueError(f"include cycle: {' -> '.join(str(seen) for seen in (*stack, path))}")
    lines, newline, mixed = _read_lines(path)
    out = []
    has_regions = False
    state = (None, 0)
    index = 0
    while index < len(lines):
        line = lines[index]
        state, opens, closes = _marker(line, *state)
        if closes is not None:
            raise ValueError(f"{rel}:{index + 1}: an include end marker with no region open")
        if opens is None:
            out.append(line)
            index += 1
            continue
        if mixed:
            raise ValueError(
                f"{rel} mixes line endings, so a region in it cannot be rendered without rewriting the rest"
            )
        if rel not in skill_documents():
            # Only the files the walk visits are written, so a region anywhere else would read filled to an includer while staying empty on disk.
            raise ValueError(
                f"{rel}:{index + 1}: an include region in a file the generator does not walk is never filled"
            )
        key = opens.group("key")
        end = None
        inner = (None, 0)
        for probe in range(index + 1, len(lines)):
            inner, nested, closing = _marker(lines[probe], *inner)
            if nested is not None:
                raise ValueError(
                    f"{rel}:{probe + 1}: an include region opens inside the one at line {index + 1}"
                )
            if closing is not None:
                end = probe
                break
        if end is None:
            raise ValueError(f"{rel}:{index + 1}: the include region for {key!r} has no end marker")
        # One blank line on each side, so a body starting with a list or a heading satisfies the blank-line rules markdownlint holds the rest of the tree to.
        out.extend([line, "", *include_body(key, (*stack, path)), "", lines[end]])
        has_regions = True
        index = end + 1
    return out, newline, has_regions


def filled_bytes(rel):
    """The bytes `rel` holds once its include regions are current, or its bytes as they are when it has none."""
    lines, newline, has_regions = filled_lines(rel)
    if not has_regions:
        return (INCLUDE_ROOT / rel).read_bytes()
    return newline.join(lines).encode("utf-8")


def skill_documents():
    """Every Markdown file under the authored tree, as root-relative POSIX paths, the files a region can sit in.

    Symlinks are refused before the walk, since a fill writes through whatever the walk found and
    a skill directory that is a symlink would put generated text into the tree it points at.
    """
    out = []
    for name in skill_names():
        reject_symlinks(SKILLS_SRC / name)
        out.extend(
            f.relative_to(INCLUDE_ROOT).as_posix()
            for f in (SKILLS_SRC / name).rglob("*.md")
            if f.is_file()
        )
    return sorted(out)


def include_drift():
    """Authored files whose include regions differ from what their sources render now.

    A hand edit inside a region and a source edit nobody regenerated for both land here. A source
    heading that no longer resolves raises instead, since regenerating cannot repair a key.
    """
    return [
        rel for rel in skill_documents() if filled_bytes(rel) != (INCLUDE_ROOT / rel).read_bytes()
    ]


def fill_includes():
    """Rewrite every authored file whose include regions are behind their sources, and return those paths."""
    changed = []
    for rel in skill_documents():
        path = INCLUDE_ROOT / rel
        rendered = filled_bytes(rel)
        if rendered != path.read_bytes():
            path.write_bytes(rendered)
            changed.append(rel)
    return changed


def regenerate():
    names = skill_names()
    # Before the copy, so the mirrors carry the filled text and the digests hash it.
    fill_includes()
    if DIST_PLUGIN.is_symlink() or DIST_PLUGIN.is_file():
        DIST_PLUGIN.unlink()
    elif DIST_PLUGIN.is_dir():
        shutil.rmtree(DIST_PLUGIN)
    dist_skills = DIST_PLUGIN / "skills"
    dist_skills.mkdir(parents=True, exist_ok=True)
    for name in names:
        reject_symlinks(SKILLS_SRC / name)
        shutil.copytree(SKILLS_SRC / name, dist_skills / name)
    if GITHUB_SKILLS.is_symlink() or GITHUB_SKILLS.is_file():
        GITHUB_SKILLS.unlink()
    elif GITHUB_SKILLS.is_dir():
        shutil.rmtree(GITHUB_SKILLS)
    GITHUB_SKILLS.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copytree(SKILLS_SRC / name, GITHUB_SKILLS / name)
    write_plugin_manifest(names)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    for name in names:
        # LF, same as write_plugin_manifest, explicit for the same Windows-platform-default reason.
        (DIGEST_DIR / name).write_text(skill_digest(name) + "\n", encoding="utf-8", newline="\n")
    return names


def is_stale():
    """Whether a generated distribution needs regenerating: missing, corrupted, or built from
    different source bytes. Checks the manifest's own content and the generated tree's actual
    bytes, not only the digest stamps, since a stamp surviving a partial deletion, a hand-edited
    manifest, or an edited-in-place generated file would otherwise report current over a plugin
    that no longer actually matches its source. Comparing the source and generated digests
    directly, rather than trusting a stamp to still describe what is on disk, is what catches
    the in-place edit: nothing else here re-reads the generated files at all.
    """
    # First, so a region behind its source reads stale however current the mirrors are, and a key that no longer resolves raises here whatever else is missing.
    if include_drift():
        return True
    if not DIGEST_DIR.is_dir() or not PLUGIN_MANIFEST.is_file():
        return True
    names = skill_names()
    try:
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupted or hand-edited manifest is exactly the stale case this function exists to catch.
        return True
    # OSError (an unreadable file, one removed between the is_file() check above and this read) is deliberately not caught here: --check's own caller needs it to propagate as the execution failure it is, not read as this function's ordinary stale result.
    # The full manifest, not only "skills".
    # A hand-edited description/author/version is exactly as much a corrupted-plugin case as a hand-edited skills list.
    # The manifest is entirely deterministic from `names`, so comparing all of it costs nothing extra to get right.
    if manifest != expected_manifest(names):
        return True
    # The digest walk below only hashes the expected names.
    # An extra directory under DIST_PLUGIN/skills/ (a retired skill left behind, one added by hand) would never be read and could not affect that comparison.
    # Checked by name first, deliberately not folded into the digest walk itself.
    dist_skills = DIST_PLUGIN / "skills"
    if not has_exact_skill_directories(dist_skills, names):
        return True
    if not has_exact_skill_directories(GITHUB_SKILLS, names):
        return True
    # Exactly one stamp per skill, checked by name for the same reason the directories are: a stamp left behind by a retired skill is never read below and could not otherwise be noticed.
    if not has_exact_entries(DIGEST_DIR, names, directories=False):
        return True
    for name in names:
        current = skill_digest(name)
        if (DIGEST_DIR / name).read_text(encoding="utf-8").strip() != current:
            return True
        if current != tree_digest(dist_skills, [name]):
            return True
        if current != tree_digest(GITHUB_SKILLS, [name]):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="read-only: exit 0 clean, 1 stale, 2 on a real failure",
    )
    args = parser.parse_args()

    if args.check:
        try:
            drift = include_drift()
            stale = is_stale()
        except (ValueError, OSError) as exc:
            # 2 rather than 1, so a caller reading the exit code (host-setup/menu.sh among them) can tell this apart from the stale result below, which also exits 1 by this flag's own documented contract.
            # OSError alongside ValueError: is_stale() reads several files beyond the one call already wrapped in its own try/except, and a permissions problem or a file removed out from under it raises that, not ValueError.
            print(exc, file=sys.stderr)
            return 2
        if drift:
            print(
                f"Include regions differ from their sources in {', '.join(drift)}:"
                " run `python3 scripts/build_dist.py`.",
                file=sys.stderr,
            )
            return 1
        if stale:
            print(
                "Generated skill distributions are stale: run `python3 scripts/build_dist.py`.",
                file=sys.stderr,
            )
            return 1
        print("Generated skill distributions are current.")
        return 0

    try:
        refreshed = include_drift()
        names = regenerate()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if refreshed:
        print(f"Include regions refreshed in {', '.join(refreshed)}.")
    print(f"{DIST_PLUGIN} regenerated from {len(names)} skill(s): {', '.join(names) or '(none)'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
