#!/usr/bin/env python3
"""Install the agent write-safety kit for the current user account. Cross-platform, idempotent.

Deploys the PreToolUse hook, registers it in the user settings.json, adds the safety rules to the user
CLAUDE.md (marker-delimited so re-runs update in place), and self-tests the hook before registering it.
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
    # expanduser so a CLAUDE_HOME set to a `~/...` form resolves to the home dir, not a literal `~` dir.
    claude_home_env = os.environ.get("CLAUDE_HOME")
    claude_home = pathlib.Path(claude_home_env).expanduser() if claude_home_env else pathlib.Path.home() / ".claude"
    hooks_dir = claude_home / "hooks"
    hook_dst = hooks_dir / "gh-write-guard.py"
    settings = claude_home / "settings.json"
    claude_md = claude_home / "CLAUDE.md"

    print(f"Installing agent write-safety kit into: {claude_home}")
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # 1. Deploy the hook and self-test it BEFORE wiring anything up.
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

    # 2. Register in settings.json: exactly one PreToolUse/Bash group carrying our hook command.
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
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    group = next((g for g in pre if g.get("matcher") == "Bash"), None)
    if group is None:
        group = {"matcher": "Bash", "hooks": []}
        pre.append(group)
    entries = group.setdefault("hooks", [])
    entries[:] = [h for h in entries if "gh-write-guard" not in str(h.get("command", ""))]
    entries.append({"type": "command", "command": hook_cmd})
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  settings -> {settings} (PreToolUse/Bash hook registered)")

    # 3. CLAUDE.md: replace the agent-safety marker block if present, else append it.
    snippet = (HERE / "claude-md-safety.md").read_text(encoding="utf-8").strip()
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    block_re = re.compile(r"<!-- agent-safety v\d+ start -->.*?<!-- agent-safety v\d+ end -->", re.S)
    if block_re.search(existing):
        updated, action = block_re.sub(lambda _: snippet, existing), "updated"
    else:
        sep = "" if existing == "" or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        updated, action = existing + sep + snippet + "\n", "appended"
    claude_md.write_text(updated, encoding="utf-8")
    print(f"  CLAUDE.md -> {claude_md} (safety block {action})")

    print("\nDone. Verify:")
    print(f"  {launcher} \"{hook_dst}\" --selftest")
    print(f"  grep -c 'agent-safety v' \"{claude_md}\"   # expect 2")
    print("Restart Claude Code sessions on this machine so the hook and CLAUDE.md load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
