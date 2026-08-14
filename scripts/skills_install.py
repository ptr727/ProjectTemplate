#!/usr/bin/env python3
"""Install the fleet's Skills for the current user account. Cross-platform, idempotent.

Two independent things happen, since Claude Code, opencode, and Codex CLI discover skills
differently (see AGENTS.md "Fleet Bootstrap" for why):

  - Codex and opencode read .agents/skills/<name>/SKILL.md directly, project-local, walking up
    from the working directory. They also check a global $HOME/.agents/skills/, which this
    installer materializes so every repo on this machine benefits, not only this checkout.
  - Claude Code never scans .agents/skills/, only .claude/skills/ or a plugin's own skills/. This
    installer registers this repo's marketplace (built at .claude-plugin/fleet-skills/, see
    build_dist.py) and installs its plugin via the `claude` CLI, so Claude Code loads the same
    content the other two tools read directly.

Both wrappers (skills_install.sh, skills_install.ps1) call this, so every OS runs one tested code path.

Every run records a stamp at ~/.agents/skills-install-stamp.json naming the machine, what was
installed, and the hub commit it came from, so staleness is checkable later without re-running the
install. `--report` reads that stamp against this checkout and answers whether the machine is
current, without changing anything.

Usage: python3 scripts/skills_install.py            (installs)
       python3 scripts/skills_install.py --report   (read-only: is this machine current?)
       AGENTS_HOME=/x python3 scripts/skills_install.py   (override the global skills target, for testing)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import build_dist

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKILLS_SRC = ROOT / ".agents" / "skills"
CLAUDE_PLUGIN_DIR = ROOT / ".claude-plugin"
MARKETPLACE_NAME = "projecttemplate-fleet"
PLUGIN_NAME = "fleet-skills"
STAMP_VERSION = 1


def agents_home():
    """Where Codex/opencode look for globally-installed skills, overridable for testing."""
    override = os.environ.get("AGENTS_HOME")
    # .expanduser(): AGENTS_HOME=~/tmp is a real thing a caller would type.
    # A bare Path() treats "~" as a literal directory name, not the shell-expanded home it looks like.
    return Path(override).expanduser() if override else Path.home() / ".agents"


def source_ref():
    """The hub commit this installer is running from, and whether the tree is dirty."""
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
        except OSError:
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    sha = git("rev-parse", "HEAD")
    if not sha:
        # A bootstrap runs this from a fetched tarball tree, which has no .git to answer for it.
        # The loader resolved its ref to a commit before downloading and hands that in, keeping the stamp checkable instead of permanently stale.
        # A tarball of a resolved commit is clean by construction, which is what dirty=False records.
        handed = os.environ.get("SKILLS_SOURCE_COMMIT")
        if handed:
            return {"vcs": "archive", "commit": handed, "dirty": False}
        return {"vcs": "none"}
    ref = {"vcs": "git", "commit": sha}
    # Watches both paths this installer actually reads.
    # .agents/skills/ is copied for Codex/opencode.
    # .claude-plugin/ is the marketplace.json and generated plugin Claude Code reads.
    # Scoping dirty to only the first missed a modified marketplace.json or generated content.
    # Repo-relative pathspecs, not str(Path).
    # With `git -C <repo>`, an absolute path can fail to match anything.
    # That silently and permanently reports dirty=False.
    watched = [SKILLS_SRC, CLAUDE_PLUGIN_DIR]
    status = git("status", "--porcelain", "--", *(p.relative_to(ROOT).as_posix() for p in watched))
    ref["dirty"] = bool(status)
    return ref


INSTALLED_MARKER = ".installed-by-projecttemplate-fleet"


def skill_source_dirs():
    """Every .agents/skills/<name>/ directory that actually carries a SKILL.md.

    Matches build_dist.skill_names()'s own definition of a skill, so a stray non-skill directory
    under .agents/skills/ (a cache folder, a scratch dir) is never treated as one here either.
    """
    if not SKILLS_SRC.is_dir():
        return []
    return [p for p in SKILLS_SRC.iterdir() if (p / "SKILL.md").is_file()]


def materialize_global_skills(target):
    """Overlay .agents/skills/ into `target`, one skill directory at a time.

    Only this fleet's own skill names are touched under `target`. `~/.agents/skills/` is a shared
    convention, not this fleet's own directory, so a machine can have skills installed there from
    other sources, and replacing the whole directory would delete those as a side effect of
    installing this fleet's skills. A plain copy rather than a symlink per skill: a symlink to a
    checkout that later moves or is deleted leaves every repo on the machine silently unable to
    resolve that skill, where a copy just goes stale (caught by --report) instead of missing
    outright.

    Raises ValueError (via build_dist.reject_symlinks) if a skill directory contains a symlink,
    since shutil.copytree() follows one by default and would silently pull in content from
    outside the tracked source tree.

    Each installed skill carries an INSTALLED_MARKER file, so a later run can tell "a fleet skill
    that got retired" apart from "someone else's skill that happens to share this directory."
    Only a directory carrying the marker is ever removed for no longer being in the current
    source set; a directory without one is never touched, retired-looking name or not.
    """
    if target.is_symlink() or target.is_file():
        target.unlink()
    target.mkdir(parents=True, exist_ok=True)
    skill_dirs = skill_source_dirs()
    current_names = {p.name for p in skill_dirs}

    for existing in target.iterdir():
        # A plain is_dir() check follows a symlink.
        # Calling shutil.rmtree() on one refuses with an uncaught OSError instead of deleting through it.
        # Confirmed locally rather than assumed.
        # Skipping a symlink here avoids that crash on a stray one under the shared target.
        if (not existing.is_symlink() and existing.is_dir()
                and existing.name not in current_names and (existing / INSTALLED_MARKER).is_file()):
            shutil.rmtree(existing)

    for skill_dir in skill_dirs:
        build_dist.reject_symlinks(skill_dir)
        dest = target / skill_dir.name
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
        shutil.copytree(skill_dir, dest)
        (dest / INSTALLED_MARKER).write_text("", encoding="utf-8")


def claude_available():
    return shutil.which("claude") is not None


def register_claude_marketplace():
    """Add this repo's marketplace and install its plugin via the `claude` CLI.

    Shells out to `claude plugin marketplace add`/`install` rather than writing
    ~/.claude/plugins/known_marketplaces.json directly: that file's shape is the CLI's own
    internal state, not a documented contract, so writing it by hand risks silently drifting from
    whatever the CLI actually expects on the next release.
    """
    marketplace_add = subprocess.run(
        ["claude", "plugin", "marketplace", "add", str(ROOT)],
        capture_output=True, text=True,
    )
    # Re-adding an already-registered marketplace is expected on a re-run.
    # Only a genuine failure (not "already exists") is fatal, since idempotence is the point.
    if (marketplace_add.returncode != 0
            and "already" not in marketplace_add.stdout.lower()
            and "already" not in marketplace_add.stderr.lower()):
        print(marketplace_add.stdout, marketplace_add.stderr, file=sys.stderr)
        return False

    install = subprocess.run(
        ["claude", "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", "--scope", "user"],
        capture_output=True, text=True,
    )
    if (install.returncode != 0
            and "already" not in install.stdout.lower()
            and "already" not in install.stderr.lower()):
        print(install.stdout, install.stderr, file=sys.stderr)
        return False
    return True


def build_stamp(claude_registered):
    return {
        "stampVersion": STAMP_VERSION,
        "hostname": socket.gethostname(),
        "source": source_ref(),
        "claudeRegistered": claude_registered,
    }


def report(stamp_path):
    if not stamp_path.is_file():
        print("Not installed on this machine (no stamp found).")
        return 1
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Stamp at {stamp_path} is unreadable ({exc}). Re-run the installer.")
        return 1
    # A non-dict JSON value (an array, a bare string) is a shape this reader does not know.
    # So is an unrecognized stampVersion, not merely a plain missing/dirty install.
    if not isinstance(stamp, dict) or stamp.get("stampVersion") != STAMP_VERSION:
        print(f"Stamp at {stamp_path} is not a recognized shape (stampVersion {STAMP_VERSION} expected). "
              "Re-run the installer.")
        return 1
    current = source_ref()
    current_commit = current.get("commit")
    # A dict-shaped stamp can still carry a non-dict "source" (a stray string, a number).
    # Calling .get("commit") on that would crash instead of reading as stale like every other unrecognized-shape case here does.
    stamp_source = stamp.get("source")
    stamp_commit = stamp_source.get("commit") if isinstance(stamp_source, dict) else None
    stamp_dirty = stamp_source.get("dirty") if isinstance(stamp_source, dict) else None
    # A dirty checkout cannot be asserted current.
    # The commit it names is not what is actually on disk.
    # A caller trusting "current" here would trust bytes that were never installed.
    # Checked on both sides.
    # The current checkout's own dirty flag catches a machine that has since gone dirty at the recorded commit.
    # The stamp's recorded dirty flag catches the install itself having happened from a dirty checkout, whose bytes a later-clean tree at the same commit can never actually reproduce or verify.
    # Wrapped in bool() rather than left as a bare "or" chain.
    # A missing current_commit (a non-git checkout) reads as stale this way.
    # A plain "or" chain would leave `stale` as the None that produces there instead, and `if stale else` treats that as falsy the same as an actual False.
    # A missing "dirty" key by itself reads as not-dirty, since get() returns None there, which is falsy.
    # That is not the same as reading as stale.
    # A call to source_ref() always sets "dirty" whenever it sets "commit", so the two are missing together only in the vcs=="none" case, which current_commit is None already catches.
    stale = bool(
        current_commit is None
        or stamp_commit != current_commit
        or current.get("dirty")
        or stamp_dirty
    )
    print(json.dumps({"stamp": stamp, "currentCommit": current_commit, "stale": stale}, indent=2))
    return 1 if stale else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="read-only: is this machine current?")
    args = parser.parse_args()

    home = agents_home()
    stamp_path = home / "skills-install-stamp.json"

    if args.report:
        return report(stamp_path)

    try:
        materialize_global_skills(home / "skills")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    claude_present = claude_available()
    claude_registered = register_claude_marketplace() if claude_present else False
    if not claude_present:
        print("`claude` not found on PATH, skipping Claude Code marketplace registration "
              "(Codex/opencode global skills were still installed).", file=sys.stderr)

    home.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(json.dumps(build_stamp(claude_registered), indent=2) + "\n", encoding="utf-8")
    print(f"Installed to {home / 'skills'}. Claude Code marketplace registered: {claude_registered}.")

    # `claude` missing is a partial-but-expected install (a Codex/opencode-only machine).
    # `claude` present but registration failing is a real failure.
    # The stamp still records it either way, but only the exit code lets automation notice.
    if claude_present and not claude_registered:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
