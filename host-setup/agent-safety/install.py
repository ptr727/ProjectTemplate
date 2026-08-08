#!/usr/bin/env python3
"""Install the agent write-safety kit for the current user account. Cross-platform, idempotent.

Deploys the PreToolUse hook, registers it in the user settings.json, merges the permission rules this
kit owns into the same file, adds the safety rules to the user CLAUDE.md (marker-delimited so re-runs
update in place), and self-tests the hook before registering it.
The bash and PowerShell wrappers both call this, so every OS runs one tested code path.

Usage: python3 install.py            (installs to ~/.claude)
       CLAUDE_HOME=/x python3 install.py   (override target, for testing)
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

# Permission rules this kit installs, each as (owned prefix, rule).
# A re-run drops every rule under the prefix before adding the current one, so a changed rule updates in place.
# The prefix bounds what the installer owns, so a rule written by hand outside it is never touched.
# These widen rather than restrict, so they stay their own step for the reason the two CLAUDE.md blocks stay separate.
MANAGED_PERMISSIONS = [
    # The review loop's reply and resolve, the one write in that loop an agent performs.
    # Driving it by hand needs a raw GraphQL mutation, which is the shape that reached a stranger's repository.
    # `pr_review.py` queries the thread id itself and takes no argument an id fits in.
    # Allowing the script is therefore narrower than allowing the mutation it replaces.
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


def main():
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
    data = {}
    if settings.exists() and settings.read_text(encoding="utf-8").strip():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
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
            "is required. Fix or remove it, then re-run. Nothing was written.\n"
        )
        return 1
    # Every container this installer descends into is checked before it is used.
    # A key holding an unexpected type would otherwise raise a traceback mid-edit.
    # That reads as a crash rather than as the settings problem it is.
    # The invalid-JSON refusal above is the shape this file already answers a malformed file with.
    for path, want in (("hooks", dict), ("hooks/PreToolUse", list),
                       ("permissions", dict), ("permissions/allow", list)):
        parts = path.split("/")
        node = data
        for p in parts[:-1]:
            node = node.get(p, {})
        held = node.get(parts[-1]) if isinstance(node, dict) else None
        if held is not None and not isinstance(held, want):
            sys.stderr.write(
                f"{settings} has `{path.replace('/', '.')}` as {type(held).__name__} where "
                f"{want.__name__} is required. Fix or remove that key, then re-run. Nothing was written.\n"
            )
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
    # Dropping the prefix before appending is the same strip-then-register the hook above uses.
    # That is what makes a re-run update a changed rule rather than leave both spellings in the list.
    # Written in the same pass as the hook, so the file is read once and written once.
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    for prefix, rule in MANAGED_PERMISSIONS:
        matched = [a for a in allow if isinstance(a, str) and a.startswith(prefix)]
        allow[:] = [a for a in allow if a not in matched] + [rule]
        # Counted over the rules actually replaced rather than over everything the prefix matched.
        # The current rule matches its own prefix, so counting the match set reports it as superseded.
        older = [a for a in matched if a != rule]
        if not matched:
            action = "added"
        elif not older:
            action = "already current"
        else:
            action = f"updated, superseding {len(older)}"
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
        block_re = re.compile(rf"<!-- {marker} v\d+ start -->.*?<!-- {marker} v\d+ end -->", re.S)
        if block_re.search(existing):
            existing, action = block_re.sub(lambda _: snippet, existing), "updated"
        else:
            sep = "" if existing == "" or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            existing, action = existing + sep + snippet + "\n", "appended"
        print(f"  CLAUDE.md -> {claude_md} ({marker} block {action})")
    claude_md.write_bytes(existing.replace("\n", newline).encode("utf-8"))

    print("\nDone. Verify:")
    print(f"  {launcher} \"{hook_dst}\" --selftest")
    print(f"  grep -c 'agent-safety v' \"{claude_md}\"      # expect 2")
    print(f"  grep -c 'fleet-bootstrap v' \"{claude_md}\"   # expect 2")
    # One line per rule, matching the rule itself rather than a word inside it.
    # A hint naming a fixed word would stop matching the moment a rule that lacks it is added.
    for _, rule in MANAGED_PERMISSIONS:
        print(f"  grep -cF '{rule}' \"{settings}\"   # expect 1")
    print("Restart Claude Code sessions on this machine so the hook and CLAUDE.md load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
