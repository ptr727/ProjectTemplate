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

The two channels hold different things. The Codex and opencode copy is a snapshot, so it keeps the
revision it was taken from. The Claude Code marketplace is a directory source that loads the hub
checkout in place, so it serves whatever that checkout holds at read time and holds no revision.

Every run records a stamp at ~/.agents/skills-install-stamp.json naming the machine, what was
installed, and the hub commit it came from. `--report` answers each channel by name, without
changing anything. For the snapshot, it says which commit the copy was taken from and whether that
is the intended revision, the promoted `main` unless `--intended` names another. For the live
channel, it says which branch and commit the registered checkout is serving now. The exit code is
the snapshot's verdict alone, since the live channel following its checkout is the design.

Usage: python3 scripts/skills_install.py            (installs)
       python3 scripts/skills_install.py --report   (read-only: what does each channel hold?)
       python3 scripts/skills_install.py --report --intended <rev>   (judge the snapshot against <rev>)
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


def git_in(root, *args):
    """Run git against `root`, returning stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def source_ref():
    """The hub commit this installer is running from, and whether the tree is dirty."""

    def git(*args):
        return git_in(ROOT, *args)

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
        if (
            not existing.is_symlink()
            and existing.is_dir()
            and existing.name not in current_names
            and (existing / INSTALLED_MARKER).is_file()
        ):
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
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    # Re-adding an already-registered marketplace is expected on a re-run.
    # Only a genuine failure (not "already exists") is fatal, since idempotence is the point.
    if (
        marketplace_add.returncode != 0
        and "already" not in marketplace_add.stdout.lower()
        and "already" not in marketplace_add.stderr.lower()
    ):
        print(marketplace_add.stdout, marketplace_add.stderr, file=sys.stderr)
        return False

    install = subprocess.run(
        ["claude", "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", "--scope", "user"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if (
        install.returncode != 0
        and "already" not in install.stdout.lower()
        and "already" not in install.stderr.lower()
    ):
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


def intended_commit(rev=None):
    """The commit the snapshot is meant to hold, and the ref that named it.

    Step 7 of merge-and-release installs from the promoted `main`, so that is the default: the
    remote-tracking ref first, as last fetched, since that step fetches it just before
    installing, then a local `main`. Nothing here fetches, so a caller whose `origin/main` may
    have moved fetches first. A fetched tarball has no git to ask, and the commit its loader
    resolved and handed in is the revision it was fetched to install.
    """
    if not rev:
        source = source_ref()
        if source.get("vcs") == "archive":
            return source["commit"], "SKILLS_SOURCE_COMMIT"
    candidates = [rev] if rev else ["refs/remotes/origin/main", "refs/heads/main"]
    for ref in candidates:
        sha = git_in(
            ROOT, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"
        )
        if sha:
            return sha, ref
    return None, None


def live_channel():
    """What the Claude Code channel serves now, read from the checkout the marketplace names.

    The checkout running this report is not necessarily the one registered, a worktree or a fresh
    clone being the ordinary cases, so the registered path is read back from the CLI.
    """
    if not claude_available():
        return {"registered": None, "reason": "`claude` not found on PATH"}
    try:
        listing = subprocess.run(
            ["claude", "plugin", "marketplace", "list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        entries = json.loads(listing.stdout) if listing.returncode == 0 else None
    except (OSError, json.JSONDecodeError):
        entries = None
    if not isinstance(entries, list):
        return {
            "registered": None,
            "reason": "`claude plugin marketplace list --json` gave no listing",
        }
    entry = next(
        (e for e in entries if isinstance(e, dict) and e.get("name") == MARKETPLACE_NAME), None
    )
    if entry is None:
        return {"registered": False}
    location = entry.get("installLocation") or entry.get("path")
    if not isinstance(location, str) or not location:
        return {"registered": True, "reason": "the listing names no location"}
    root = Path(location)
    # Only the generated plugin tree is what this channel loads, so only it decides dirty here.
    status = git_in(
        root, "status", "--porcelain", "--", CLAUDE_PLUGIN_DIR.relative_to(ROOT).as_posix()
    )
    return {
        "registered": True,
        "checkout": str(root),
        # None on a detached HEAD, which serves a commit rather than a branch.
        "branch": git_in(root, "symbolic-ref", "--quiet", "--short", "HEAD"),
        "commit": git_in(root, "rev-parse", "HEAD"),
        "dirty": None if status is None else bool(status),
    }


def report(stamp_path, intended_rev=None):
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
        print(
            f"Stamp at {stamp_path} is not a recognized shape (stampVersion {STAMP_VERSION} expected). "
            "Re-run the installer."
        )
        return 1
    # A dict-shaped stamp can still carry a non-dict "source" (a stray string, a number).
    # Calling .get("commit") on that would crash instead of reading as not current like every other unrecognized-shape case here does.
    stamp_source = stamp.get("source")
    stamp_commit = stamp_source.get("commit") if isinstance(stamp_source, dict) else None
    stamp_dirty = stamp_source.get("dirty") if isinstance(stamp_source, dict) else None
    intended, intended_ref = intended_commit(intended_rev)
    # A copy installed from a dirty checkout holds bytes no commit reproduces, so it is never current.
    # Wrapped in bool() so a missing commit on either side reads as False rather than as a falsy None.
    current = bool(intended and stamp_commit == intended and not stamp_dirty)
    snapshot = {
        "installedFrom": stamp_commit,
        "installedDirty": stamp_dirty,
        "intended": intended,
        "intendedRef": intended_ref if intended else intended_rev,
        "current": current,
    }
    print(json.dumps({"stamp": stamp, "snapshot": snapshot, "live": live_channel()}, indent=2))
    return 0 if current else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", action="store_true", help="read-only: what does each channel hold?"
    )
    parser.add_argument(
        "--intended",
        metavar="REV",
        help="with --report: the revision the snapshot should hold (default: the promoted main)",
    )
    args = parser.parse_args()
    if args.intended and not args.report:
        parser.error("--intended only applies with --report")

    home = agents_home()
    stamp_path = home / "skills-install-stamp.json"

    if args.report:
        return report(stamp_path, args.intended)

    try:
        materialize_global_skills(home / "skills")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    claude_present = claude_available()
    claude_registered = register_claude_marketplace() if claude_present else False
    if not claude_present:
        print(
            "`claude` not found on PATH, skipping Claude Code marketplace registration "
            "(Codex/opencode global skills were still installed).",
            file=sys.stderr,
        )

    home.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(
        json.dumps(build_stamp(claude_registered), indent=2) + "\n", encoding="utf-8"
    )
    # Two independent operations get separate lines, not one combined sentence.
    print(f"Skills materialized to {home / 'skills'}.")
    print(f"Claude Code marketplace registered: {claude_registered}.")

    # `claude` missing is a partial-but-expected install (a Codex/opencode-only machine).
    # `claude` present but registration failing is a real failure.
    # The stamp still records it either way, but only the exit code lets automation notice.
    if claude_present and not claude_registered:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
