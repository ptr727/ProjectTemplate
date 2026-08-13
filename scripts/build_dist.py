#!/usr/bin/env python3
"""Generate the Claude-plugin-compatible copy of .agents/skills/ at .claude-plugin/fleet-skills/.

.agents/skills/ is the one hand-authored source: Codex and opencode read it directly with no
install step. Claude Code never scans that path, only .claude/skills/ or a plugin's own skills/
directory, so this script materializes a plugin (.claude-plugin/fleet-skills/) that
.claude-plugin/marketplace.json publishes, keeping .agents/skills/ the single place a skill's
content is ever hand-edited. Nested under .claude-plugin/ rather than a top-level dist/, since
this repo's .gitignore already gives dist/ a different, Python-build-artifact meaning.

Usage: python3 scripts/build_dist.py           regenerate the plugin from .agents/skills/
       python3 scripts/build_dist.py --check   read-only: exit 1 if the plugin is stale
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_SRC = ROOT / ".agents" / "skills"
PLUGIN_NAME = "fleet-skills"
DIST_PLUGIN = ROOT / ".claude-plugin" / PLUGIN_NAME
PLUGIN_MANIFEST = DIST_PLUGIN / ".claude-plugin" / "plugin.json"
DIGEST_STAMP = DIST_PLUGIN / ".source-digest"


def skill_names():
    """Every .agents/skills/<name>/ directory that carries a SKILL.md, sorted for a stable digest."""
    if not SKILLS_SRC.is_dir():
        return []
    return sorted(p.name for p in SKILLS_SRC.iterdir() if (p / "SKILL.md").is_file())


def reject_symlinks(skill_dir):
    """Raise if `skill_dir` contains a symlink anywhere in its tree.

    .agents/skills/ is the one hand-authored source, with no legitimate reason to hold a symlink.
    Path.is_file() and read_bytes() both dereference one, and so does shutil.copytree() by
    default, so an unnoticed symlink there would let the digest and the generated plugin silently
    include content from outside this tree, tracked or not.
    """
    for p in skill_dir.rglob("*"):
        if p.is_symlink():
            raise ValueError(f"{p} is a symlink; .agents/skills/ must contain only real files")


def source_digest(names):
    """One digest over every source file for the given skills, order-independent per skill.

    Names are hashed in the fixed sorted order the caller already produced. Each skill's own files
    are hashed in a second stable sort so an unrelated filesystem listing order never changes the
    digest on a machine where nothing changed.
    """
    h = hashlib.sha256()
    for name in names:
        reject_symlinks(SKILLS_SRC / name)
        h.update(name.encode("utf-8"))
        for f in sorted((SKILLS_SRC / name).rglob("*")):
            if f.is_file():
                # .as_posix(), not str(): a bare str(Path) uses backslashes on Windows.
                # That would make the digest disagree with a Linux machine over identical bytes.
                h.update(f.relative_to(SKILLS_SRC).as_posix().encode("utf-8"))
                h.update(f.read_bytes())
    return h.hexdigest()[:16]


def write_plugin_manifest(names):
    PLUGIN_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": PLUGIN_NAME,
        "version": "0.0.0",
        "description": "Fleet-wide agent rules (comment style, PR-review conduct, resync safety) "
        "packaged as Claude Code Skills, generated from .agents/skills/.",
        "author": {"name": "ptr727"},
        "skills": [f"./skills/{name}" for name in names],
    }
    # CRLF, matching this repo's JSON default (.editorconfig `[*] end_of_line = crlf`).
    # No LF pin applies here, since this is not a shebang-executed or shell-consumed path.
    PLUGIN_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\r\n")


def regenerate():
    names = skill_names()
    if DIST_PLUGIN.is_symlink() or DIST_PLUGIN.is_file():
        DIST_PLUGIN.unlink()
    elif DIST_PLUGIN.is_dir():
        shutil.rmtree(DIST_PLUGIN)
    dist_skills = DIST_PLUGIN / "skills"
    dist_skills.mkdir(parents=True, exist_ok=True)
    for name in names:
        reject_symlinks(SKILLS_SRC / name)
        shutil.copytree(SKILLS_SRC / name, dist_skills / name)
    write_plugin_manifest(names)
    # CRLF, same as write_plugin_manifest.
    # The platform default would write plain LF on Linux.
    # That disagrees with this repo's CRLF default for a path no LF pin covers.
    DIGEST_STAMP.write_text(source_digest(names) + "\n", encoding="utf-8", newline="\r\n")
    return names


def is_stale():
    """Whether the generated plugin needs regenerating: missing, corrupted, or built from
    different source bytes. Checks the manifest's own content and each skill's directory too,
    not only the digest stamp, since a stamp surviving a partial deletion or a hand-edited
    manifest would otherwise report current over a plugin that no longer actually resolves."""
    if not DIGEST_STAMP.is_file() or not PLUGIN_MANIFEST.is_file():
        return True
    names = skill_names()
    try:
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if manifest.get("skills") != [f"./skills/{name}" for name in names]:
        return True
    if any(not (DIST_PLUGIN / "skills" / name / "SKILL.md").is_file() for name in names):
        return True
    return DIGEST_STAMP.read_text(encoding="utf-8").strip() != source_digest(names)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="read-only: exit 1 if the generated plugin is stale")
    args = parser.parse_args()

    if args.check:
        try:
            stale = is_stale()
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        if stale:
            print(f"{DIST_PLUGIN} is stale: run `python3 scripts/build_dist.py`.", file=sys.stderr)
            return 1
        print(f"{DIST_PLUGIN} is current.")
        return 0

    try:
        names = regenerate()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{DIST_PLUGIN} regenerated from {len(names)} skill(s): {', '.join(names) or '(none)'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
