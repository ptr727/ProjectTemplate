#!/usr/bin/env python3
"""Install the agent write-safety kit for the current user account. Cross-platform, idempotent.

Deploys the PreToolUse hook, registers it in the user settings.json, merges the permission rules this
kit owns into the same file, adds the safety rules to the user CLAUDE.md (marker-delimited so re-runs
update in place), and self-tests the hook before registering it.
The bash and PowerShell wrappers both call this, so every OS runs one tested code path.

Every run records a stamp at ~/.claude/agent-safety-stamp.json naming the machine, what was
installed, and the hub commit it came from, so a fleet rollout can be tracked from the hosts
rather than from memory. `--report` reads that stamp against this checkout and answers whether
the machine is current, without changing anything.

Usage: python3 install.py            (installs to ~/.claude)
       python3 install.py --report   (read-only: is this machine current?)
       CLAUDE_HOME=/x python3 install.py   (override target, for testing)
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

# The stamp's own format version, separate from the content it describes.
# A reader that predates a field needs to know the shape changed rather than infer it from a missing key.
STAMP_VERSION = 1

# The files whose bytes this kit actually places on a machine.
# The digest is taken over these rather than over the commit, since it is the content that runs.
# A clean commit and a dirty checkout install different bytes while reporting the same SHA.
PAYLOAD_FILES = ("gh-write-guard.py", "claude-md-safety.md", "claude-md-fleet.md")

# Distinguishes an absent key from one holding an explicit null, which `dict.get` reports alike.
# The two need different answers, since a gap is filled and a null is a settings error.
MISSING = object()


def owns(entry, prefix):
    """Whether an allow rule names the script the prefix identifies, rather than a longer path.

    The prefix ends at the script name, so a bare `startswith` also claims `pr_review.py-custom`,
    and dropping that would delete a hand-written rule for a different script. What separates the
    two is the character after the name: a rule that invokes this script continues with a rule-syntax
    delimiter, where a different script continues with more of its own path.
    """
    return entry.startswith(prefix) and entry[len(prefix):len(prefix) + 1] in (":", " ", ")")


def at(data, path):
    """The value at a slash-separated key path, or MISSING where any step of it is absent."""
    node = data
    for part in path.split("/"):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node

# Permission rules this kit installs, each as (owned prefix, rule).
# A re-run drops every rule the prefix owns before adding the current one, so a changed rule updates in place.
# Ownership needs a delimiter after the prefix, so a longer path such as `pr_review.py-custom` is not claimed.
# These widen rather than restrict, so they stay their own step for the reason the two CLAUDE.md blocks stay separate.
MANAGED_PERMISSIONS = [
    # The review loop's reply and resolve, the one write in that loop an agent performs.
    # Driving it by hand needs a raw GraphQL mutation carrying a node id, which is the shape to avoid.
    # The rule decides which command skips a prompt, and it bounds no checkout, since it matches the text.
    ("Bash(python3 scripts/pr_review.py", "Bash(python3 scripts/pr_review.py:*)"),
]


def hook_launcher():
    """A python invocation for the settings.json command. Prefer a bare `python3` (portable and
    unambiguously Python 3), else this interpreter's absolute path (guaranteed the Python 3 running the
    installer). Never a bare `python`, which is Python 2 on some systems and would fail the hook's
    Python 3 syntax."""
    if shutil.which("python3"):
        return "python3"
    return sys.executable


def host_facts():
    """Name and kind of this machine, enough to tell one host in the fleet from another.

    The distro is read from /etc/os-release rather than from `platform`, which reports the kernel
    and cannot tell Debian from Ubuntu. WSL is named because it is a distinct rollout target that
    otherwise reports as the Linux it runs.
    """
    facts = {"hostname": socket.gethostname(), "system": platform.system(), "release": platform.release()}
    osr = pathlib.Path("/etc/os-release")
    if osr.exists():
        fields = {}
        for line in osr.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key] = value.strip().strip('"')
        if fields.get("PRETTY_NAME"):
            facts["distro"] = fields["PRETTY_NAME"]
    if "microsoft" in platform.release().lower():
        facts["wsl"] = True
    return facts


def source_ref():
    """The hub commit this installer is running from, and whether the tree is dirty.

    A dirty tree is reported rather than hidden: the SHA still names a commit, but the bytes
    installed are not that commit's, and a stamp that claims otherwise is the thing this exists
    to prevent. A checkout that is not a git tree at all (an extracted tarball) says so.
    """
    def git(*args):
        # A host with no git is the normal case for a tarball install, and it is not an error here.
        # Letting FileNotFoundError escape would crash both the install and the read-only report.
        try:
            r = subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True)
        except OSError:
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    sha = git("rev-parse", "HEAD")
    if not sha:
        return {"vcs": "none"}
    ref = {"vcs": "git", "commit": sha}
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        ref["branch"] = branch
    status = git("status", "--porcelain", "--", *PAYLOAD_FILES)
    ref["dirty"] = bool(status)
    return ref


def payload_digest():
    """One digest over the content this kit installs, normalized the way the installer writes it.

    Over raw bytes this reported drift a reinstall could not clear: the snippets are embedded with
    `.strip()`, so a trailing newline moved the digest while the installed block stayed identical,
    and the machine was told to re-run something that would write the same file. Line endings
    normalize for the same reason.

    Fixed order because a set of files has none, and a digest that depends on directory listing
    order reports drift on a machine where nothing changed. The order matches the one
    `installed_digest` reads, so the two are directly comparable: the hook, then each block.
    """
    h = hashlib.sha256()
    for name in PAYLOAD_FILES:
        raw = (HERE / name).read_bytes().replace(b"\r\n", b"\n")
        # A snippet is embedded stripped, so trailing whitespace is not installed content.
        # The hook is copied byte for byte, so nothing about it is stripped.
        if name.endswith(".md"):
            raw = raw.decode("utf-8").strip().encode("utf-8")
        h.update(raw)
    return h.hexdigest()[:16]


def blocks_present(claude_md):
    """The marker version of each block actually in CLAUDE.md, by name.

    Read from the file rather than from what the installer meant to write, since the question the
    stamp answers is what is on the machine.
    """
    if not claude_md.exists():
        return {}
    text = claude_md.read_text(encoding="utf-8", errors="replace")
    found = {}
    for marker in ("agent-safety", "fleet-bootstrap"):
        # A start marker alone is a half-written block, which a presence check reads as installed.
        # Exactly one pair, since the installer writes one and a duplicate is a corrupted file.
        # Two blocks mean the second silently governs, and reporting the first as current hides that.
        starts = re.findall(rf"<!-- {marker} (v\d+) start -->", text)
        ends = re.findall(rf"<!-- {marker} (v\d+) end -->", text)
        if len(starts) == 1 and starts == ends:
            found[marker] = starts[0]
    return found


def installed_digest(claude_home):
    """A digest over the bytes actually on this machine, or None where the kit is not fully there.

    Markers and versions answer whether a block is present, and nothing about its content, so a
    block edited between its own markers reports current under a presence check. The hook is not
    marker-delimited at all, so a modified or deleted one is invisible the same way.

    Line endings are normalized first: CLAUDE.md keeps whatever endings it had, and a machine that
    holds identical text with CRLF is current rather than drifted.
    """
    hook = claude_home / "hooks" / "gh-write-guard.py"
    claude_md = claude_home / "CLAUDE.md"
    if not hook.is_file() or not claude_md.is_file():
        return None
    h = hashlib.sha256()
    h.update(hook.read_bytes().replace(b"\r\n", b"\n"))
    text = claude_md.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    for marker in ("agent-safety", "fleet-bootstrap"):
        found = re.search(rf"<!-- {marker} v\d+ start -->.*?<!-- {marker} v\d+ end -->", text, re.DOTALL)
        if not found:
            return None
        h.update(found.group(0).encode("utf-8"))
    return h.hexdigest()[:16]




def build_stamp(claude_home, installed):
    """The record written to the machine after an install, or computed live for a report."""
    return {
        "stampVersion": STAMP_VERSION,
        "host": host_facts(),
        "source": source_ref(),
        "payloadDigest": payload_digest(),
        "installedDigest": installed_digest(claude_home),
        "blocks": blocks_present(claude_home / "CLAUDE.md"),
        "installedUtc": installed,
    }


# Checked before a stamp is read, so a hand-edited or older-format file gives a verdict rather than a traceback.
# A partial write produces valid JSON with keys missing.
STAMP_REQUIRED = ("host", "source", "payloadDigest", "blocks", "installedUtc")


def stamp_line(stamp):
    """One line naming the machine and what it carries, short enough to paste into a checklist."""
    host = stamp["host"]
    src = stamp["source"]
    where = host.get("distro") or f"{host['system']} {host['release']}"
    if host.get("wsl"):
        where += " (WSL)"
    commit = src.get("commit", "unknown")[:7] + ("-dirty" if src.get("dirty") else "")
    blocks = ", ".join(f"{k} {v}" for k, v in sorted(stamp["blocks"].items())) or "none"
    return f"{host['hostname']} | {where} | hub {commit} | payload {stamp['payloadDigest']} | {blocks} | {stamp['installedUtc']}"


def report(claude_home):
    """Answer whether this machine matches this checkout, reading only.

    Compared on the payload digest rather than on the commit, because a machine installed from an
    older commit whose kit bytes never changed is current, and reporting it as stale sends someone
    to re-run an installer that would write the same file.
    """
    path = claude_home / "agent-safety-stamp.json"
    current = payload_digest()
    print(f"This checkout: payload {current}, hub {source_ref().get('commit', 'unknown')[:7]}")
    if not path.exists():
        print(f"NOT INSTALLED: no stamp at {path}")
        print("  Run the installer with no arguments to install and stamp this machine.")
        return 2
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"Stamp at {path} is unreadable ({e}). Re-run the installer to rewrite it.\n")
        return 2
    # Valid JSON is not a usable stamp: a hand edit or an older format parses and then breaks the read.
    missing = [k for k in STAMP_REQUIRED if k not in stamp] if isinstance(stamp, dict) else ["everything"]
    if missing:
        sys.stderr.write(f"Stamp at {path} is missing {', '.join(missing)}. "
                         "Re-run the installer to rewrite it.\n")
        return 2
    print(f"This machine:  {stamp_line(stamp)}")
    # The stamp says what was installed; the machine says what is there now.
    # A block edited or deleted by hand since the install makes both true and only the second current.
    live = blocks_present(claude_home / "CLAUDE.md")
    problems = []
    if stamp.get("payloadDigest") != current:
        problems.append("payload digest differs from this checkout")
    # Markers answer presence and say nothing about content, so the installed bytes are compared too.
    # This is what catches a block edited between its own markers, and a modified or deleted hook.
    live_installed = installed_digest(claude_home)
    if live_installed is None:
        problems.append("the deployed hook or CLAUDE.md is missing, so the kit is not fully installed")
    elif live_installed != current:
        problems.append("the installed content differs from what this checkout would write")
    if live != stamp.get("blocks"):
        problems.append(f"CLAUDE.md now holds {live or 'no blocks'}, where the stamp recorded {stamp.get('blocks') or 'none'}")
    if stamp.get("source", {}).get("dirty"):
        problems.append("installed from a dirty checkout, so the recorded commit does not identify the bytes")
    if problems:
        print("STALE:")
        for p in problems:
            print(f"  - {p}")
        print("  Re-run the installer with no arguments. It is idempotent.")
        return 1
    print("CURRENT: this machine matches this checkout.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Install the agent write-safety kit, or report whether this machine is current.")
    parser.add_argument("--report", action="store_true",
                        help="read-only: compare this machine's stamp against this checkout and exit")
    args = parser.parse_args()

    if sys.version_info < (3, 7):
        sys.stderr.write("This installer and the hook require Python 3.7+. Run it with python3.\n")
        return 1
    # Expanduser resolves a CLAUDE_HOME set in `~/...` form to the home dir rather than a literal `~` dir.
    claude_home_env = os.environ.get("CLAUDE_HOME")
    claude_home = pathlib.Path(claude_home_env).expanduser() if claude_home_env else pathlib.Path.home() / ".claude"
    hooks_dir = claude_home / "hooks"
    hook_dst = hooks_dir / "gh-write-guard.py"
    settings = claude_home / "settings.json"
    claude_md = claude_home / "CLAUDE.md"

    # Reported before anything is created, so a report on an uninstalled machine does not install it.
    if args.report:
        return report(claude_home)

    print(f"Installing agent write-safety kit into: {claude_home}")
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # 1. Deploy the hook, then self-test it before wiring anything up.
    shutil.copyfile(HERE / "gh-write-guard.py", hook_dst)
    try:
        os.chmod(hook_dst, 0o755)
    except OSError:
        pass
    print(f"  hook -> {hook_dst}")
    r = subprocess.run([sys.executable, str(hook_dst), "--selftest"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write("Hook self-test FAILED; aborting before registration.\n" + r.stdout + r.stderr)
        return 1
    print("  hook self-test: PASS")

    # 2. Register the hook command in settings.json under exactly one PreToolUse/Bash group.
    launcher = hook_launcher()
    # Quote the launcher too: the sys.executable fallback can contain spaces (e.g. C:\Program Files\...).
    hook_cmd = f'"{launcher}" "{hook_dst}"'
    # Read into a variable rather than twice off disk, once to test for content and once to parse.
    # Two reads can also disagree, since another process may write between them.
    data = {}
    raw = settings.read_text(encoding="utf-8") if settings.exists() else ""
    if raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.stderr.write(
                f"{settings} exists but is not valid JSON ({e}). Fix or remove it, then re-run.\n"
            )
            return 1
    # A settings file is an object, and any other JSON value parses cleanly and breaks every lookup below.
    # The root is therefore checked before the keys under it are.
    if not isinstance(data, dict):
        sys.stderr.write(
            f"{settings} is valid JSON but holds {type(data).__name__} at its root where an object "
            "is required. Fix or remove it, then re-run. This file is unchanged, so the hook is "
            "deployed but not registered.\n"
        )
        return 1
    # Every container this installer descends into is checked before it is used.
    # A key holding an unexpected type would otherwise raise a traceback mid-edit.
    # That reads as a crash rather than as the settings problem it is.
    # The invalid-JSON refusal above is the shape this file already answers a malformed file with.
    def reject(where, held, want):
        sys.stderr.write(
            f"{settings} has `{where}` as {type(held).__name__} where {want.__name__} is required. "
            "Fix or remove that key, then re-run. This file is unchanged, so the hook is deployed "
            "but not registered.\n"
        )

    for path, want in (("hooks", dict), ("hooks/PreToolUse", list),
                       ("permissions", dict), ("permissions/allow", list)):
        held = at(data, path)
        # An explicit null is present rather than absent, and `setdefault` hands back the null it found.
        # It is therefore rejected here rather than read as a gap the default fills.
        if held is not MISSING and not isinstance(held, want):
            reject(path.replace("/", "."), held, want)
            return 1

    # A list of the right type can still hold the wrong elements.
    # The registration below reads each group as an object, and each group's `hooks` as a list it appends to.
    groups = at(data, "hooks/PreToolUse")
    if groups is not MISSING:
        for i, g in enumerate(groups):
            if not isinstance(g, dict):
                reject(f"hooks.PreToolUse[{i}]", g, dict)
                return 1
            if "hooks" in g and not isinstance(g["hooks"], list):
                reject(f"hooks.PreToolUse[{i}].hooks", g["hooks"], list)
                return 1
            for j, h in enumerate(g.get("hooks") or []):
                if not isinstance(h, dict):
                    reject(f"hooks.PreToolUse[{i}].hooks[{j}]", h, dict)
                    return 1

    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    # Strip the hook from every existing group first, so a re-run leaves no duplicate behind.
    # That matters when settings.json already carries more than one Bash group.
    # Then register it in a single Bash group.
    for g in pre:
        hooks_list = g.get("hooks")
        if isinstance(hooks_list, list):
            hooks_list[:] = [h for h in hooks_list if "gh-write-guard" not in str(h.get("command", ""))]
    group = next((g for g in pre if g.get("matcher") == "Bash"), None)
    if group is None:
        group = {"matcher": "Bash", "hooks": []}
        pre.append(group)
    group.setdefault("hooks", []).append({"type": "command", "command": hook_cmd})
    done = ["PreToolUse/Bash hook registered"]

    # 3. Permission rules, merged under the prefixes this installer owns.
    # The strip-then-register shape is the hook registration's above, applied to a flat list.
    # Written in the same pass as the hook, so the file is read once and written once.
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    for prefix, rule in MANAGED_PERMISSIONS:
        matched = [a for a in allow if isinstance(a, str) and owns(a, prefix)]
        allow[:] = [a for a in allow if a not in matched] + [rule]
        # Counted over what the write removes rather than over what the prefix matched.
        # The current rule matches its own prefix, so a match-set count reports it as superseded.
        # A duplicate of it is removed too, and both can happen at once, so both are named.
        older = [a for a in matched if a != rule]
        duplicates = max(0, len(matched) - len(older) - 1)
        changes = []
        if older:
            changes.append(f"superseding {len(older)}")
        if duplicates:
            changes.append(f"removing {duplicates} duplicate" + ("s" if duplicates > 1 else ""))
        if not matched:
            action = "added"
        elif changes:
            action = "updated, " + " and ".join(changes)
        else:
            action = "already current"
        done.append(f"permission {rule}: {action}")

    # Reported after the write rather than as each edit is made, since both edits share one write.
    # A line printed before it claims a change that a later failure would leave unmade.
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for line in done:
        print(f"  settings -> {settings} ({line})")

    # 4. CLAUDE.md carries one marker block per snippet, replaced where present and appended where not.
    # The two blocks install and update independently, so one can change without rewriting the other.
    # The safety block states restrictions only.
    # The fleet block enables, so it stays separate from a block whose own text says nothing in it widens a permission.
    blocks = [("agent-safety", "claude-md-safety.md"), ("fleet-bootstrap", "claude-md-fleet.md")]
    # Preserve CLAUDE.md's existing line endings: work in \n internally, write back with its own ending.
    if claude_md.exists():
        raw = claude_md.read_bytes()
        newline = "\r\n" if b"\r\n" in raw else "\n"
        existing = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    else:
        newline, existing = "\n", ""
    for marker, filename in blocks:
        snippet = (HERE / filename).read_text(encoding="utf-8").strip()
        block_re = re.compile(rf"<!-- {marker} v\d+ start -->.*?<!-- {marker} v\d+ end -->", re.DOTALL)
        if block_re.search(existing):
            existing, action = block_re.sub(lambda _: snippet, existing), "updated"
        else:
            sep = "" if existing == "" or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            existing, action = existing + sep + snippet + "\n", "appended"
        print(f"  CLAUDE.md -> {claude_md} ({marker} block {action})")
    claude_md.write_bytes(existing.replace("\n", newline).encode("utf-8"))

    # 5. Stamp the machine, written last so it records a completed install rather than an attempted one.
    # The blocks are read back off disk here, so the stamp reports what CLAUDE.md holds rather than what was intended.
    stamp_path = claude_home / "agent-safety-stamp.json"
    stamp = build_stamp(claude_home, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    stamp_path.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    print(f"  stamp -> {stamp_path}")

    print("\nDone. This machine:")
    print(f"  {stamp_line(stamp)}")
    print("\nRe-check at any time, from a fresh hub checkout, without changing anything:")
    print(f"  {launcher} \"{HERE / 'install.py'}\" --report")
    print("Restart Claude Code sessions on this machine so the hook and CLAUDE.md load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
