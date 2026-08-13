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


def materialize_global_skills(target):
    """Overlay .agents/skills/ into `target`, one skill directory at a time.

    Only this fleet's own skill names are touched under `target`. `~/.agents/skills/` is a shared
    convention, not this fleet's own directory, so a machine can have skills installed there from
    other sources, and replacing the whole directory would delete those as a side effect of
    installing this fleet's skills. A plain copy rather than a symlink per skill: a symlink to a
    checkout that later moves or is deleted leaves every repo on the machine silently unable to
    resolve that skill, where a copy just goes stale (caught by --report) instead of missing
    outright.
    """
    if target.is_symlink() or target.is_file():
        target.unlink()
    target.mkdir(parents=True, exist_ok=True)
    if not SKILLS_SRC.is_dir():
        return
    for skill_dir in SKILLS_SRC.iterdir():
        if not skill_dir.is_dir():
            continue
        dest = target / skill_dir.name
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
        shutil.copytree(skill_dir, dest)


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
    if marketplace_add.returncode != 0 and "already" not in marketplace_add.stdout.lower() \
            and "already" not in marketplace_add.stderr.lower():
        print(marketplace_add.stdout, marketplace_add.stderr, file=sys.stderr)
        return False

    install = subprocess.run(
        ["claude", "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", "--scope", "user"],
        capture_output=True, text=True,
    )
    if install.returncode != 0 and "already" not in install.stdout.lower() \
            and "already" not in install.stderr.lower():
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
    # A dirty checkout cannot be asserted current.
    # The commit it names is not what is actually on disk.
    # A caller trusting "current" here would trust bytes that were never installed.
    stale = stamp.get("source", {}).get("commit") != current.get("commit") or current.get("dirty")
    print(json.dumps({"stamp": stamp, "currentCommit": current.get("commit"), "stale": stale}, indent=2))
    return 1 if stale else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="read-only: is this machine current?")
    args = parser.parse_args()

    home = agents_home()
    stamp_path = home / "skills-install-stamp.json"

    if args.report:
        return report(stamp_path)

    materialize_global_skills(home / "skills")

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
