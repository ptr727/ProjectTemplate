#!/usr/bin/env python3
"""PreToolUse guard: deny a subagent dispatch at the maintainer-only model tier.

Registered as a Claude Code PreToolUse hook on the Agent and Workflow tools. It reads the
tool-input JSON on stdin. It prints a deny decision when the call names the model of the tier
`model-tiers.json` marks maintainer-only, today `highest`, so `fable`. Everything else passes.

The mapping is read from `model-tiers.json` beside this file, which the installer copies from the
hub's spec/model-tiers.json. A model release is then an edit to that file alone.

The maintainer allows the tier for a session by launching it with FLEET_ALLOW_HIGHEST=1 in the
environment. A hook runs as its own process, so an agent cannot set it for itself, and the deny
text never suggests that it try. The value must be exactly `1`, so a loose truthy spelling stays
denied.

It bounds a good-faith agent's mistake rather than an adversary. A workflow script is read as
text, so a model chosen by a computed expression is not seen. A saved workflow named by `name`
alone cannot be inspected and is allowed. So is a child workflow a script starts by name or path,
since only the top-level script is read. A dispatch naming no model, and a fork whatever it names,
runs at a model this hook cannot read, so it is allowed.

A config that is missing, unreadable, or lacks the keys cannot say which model is blocked. A call
naming any model is then denied, so the guard never goes quiet without saying so, and a call naming
none is allowed.

Run `model-tier-guard.py --selftest` to check the decision matrix without Claude Code.
"""

import json
import os
import re
import sys

CONFIG_NAME = "model-tiers.json"
ALLOW_ENV = "FLEET_ALLOW_HIGHEST"
TOOL = "claude-code"


def load_tiers(path):
    """The (blocked model, default tier, default model) triple from the config, or None where it cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        models = cfg["tools"][TOOL]["models"]
        blocked = models[cfg["maintainerOnly"]]
        default_tier = cfg["defaultTier"]
        default = models[default_tier]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(blocked, str) or not isinstance(default, str) or not blocked:
        return None
    return blocked, default_tier, default


def script_text(tool_input, cwd=None):
    """`(text, unreadable)` for the script the Workflow tool will run, the file at `scriptPath` and the inline `script`.

    Both are read and joined, since a call passing both runs the file. Reading only the inline
    text would let a model named in the file pass unseen. A relative path resolves against the
    session's `cwd` rather than this hook's own directory. A file that cannot be read is reported,
    so the caller refuses rather than passing a script it never saw.
    """
    parts = []
    unreadable = False
    path = tool_input.get("scriptPath")
    if isinstance(path, str) and path:
        path = os.path.expanduser(path)
        if not os.path.isabs(path) and isinstance(cwd, str) and cwd:
            path = os.path.join(cwd, path)
        try:
            with open(path, encoding="utf-8") as f:
                parts.append(f.read())
        except (OSError, ValueError):
            unreadable = True
    inline = tool_input.get("script")
    if isinstance(inline, str):
        parts.append(inline)
    return "\n".join(parts), unreadable


def names_model(text, model):
    """Whether a script text carries a `model: '<name>'` option naming `model`."""
    # A JavaScript key may be bare or quoted, so `{"model": "x"}` is the same option as `{model: "x"}`.
    # The value may be a full model id rather than the alias, so the alias is matched as a word inside it.
    value = r"[^'\"`]*(?<![a-z0-9])(?i:" + re.escape(model) + r")(?![a-z0-9])[^'\"`]*"
    pattern = r"(?<![\w$])['\"]?model['\"]?\s*:\s*['\"`]" + value + r"['\"`]"
    return re.search(pattern, text) is not None


def any_model(text):
    """Whether a script text names any model option at all, for the unreadable-config case."""
    return re.search(r"(?<![\w$])['\"]?model['\"]?\s*:\s*['\"`]", text) is not None


def classify(tool_name, tool_input, tiers, environ, cwd=None):
    """Return ("deny", reason) or ("allow", "") for one tool call."""
    if tool_name not in ("Agent", "Workflow") or not isinstance(tool_input, dict):
        return "allow", ""
    if environ.get(ALLOW_ENV) == "1":
        return "allow", ""
    if tool_name == "Agent":
        model = tool_input.get("model")
        model = model if isinstance(model, str) else ""
    else:
        model = ""
    text, unreadable = script_text(tool_input, cwd) if tool_name == "Workflow" else ("", False)
    if unreadable:
        return "deny", (
            "The workflow's scriptPath could not be read, so a model it names cannot be ruled out. "
            "Pass an absolute path to a readable file, or the script inline."
        )
    if tiers is None:
        if model or any_model(text):
            return "deny", (
                f"{CONFIG_NAME} is missing or unreadable beside this hook, so the maintainer-only "
                "model tier cannot be read and a dispatch naming a model is refused. Re-run the "
                "agent-safety installer to restore it."
            )
        return "allow", ""
    blocked, default_tier, default = tiers
    hit = model.lower() == blocked.lower() if model else names_model(text, blocked)
    if not hit:
        return "allow", ""
    return "deny", (
        f'The `{blocked}` model is the maintainer-only tier (AGENTS.md "Model Tiers") and this '
        "session was not launched with it allowed. Where the maintainer asked for this tier, stop "
        "and report the refusal rather than dispatching lower. Otherwise dispatch at the tier the "
        f"task calls for, `{default_tier}` (`{default}`) by default."
    )


def _selftest():
    tiers = ("fable", "frontier", "opus")
    cases = [
        ("Agent", {"model": "fable"}, tiers, {}, "deny"),
        ("Agent", {"model": "Fable"}, tiers, {}, "deny"),
        ("Agent", {"model": "opus"}, tiers, {}, "allow"),
        ("Agent", {}, tiers, {}, "allow"),
        ("Agent", {"model": "fable"}, tiers, {ALLOW_ENV: "1"}, "allow"),
        ("Agent", {"model": "fable"}, tiers, {ALLOW_ENV: "true"}, "deny"),
        ("Workflow", {"script": "agent('x', { model: 'fable' })"}, tiers, {}, "deny"),
        ("Workflow", {"script": 'agent("x", {model:"opus"})'}, tiers, {}, "allow"),
        ("Workflow", {"name": "saved"}, tiers, {}, "allow"),
        ("Bash", {"model": "fable"}, tiers, {}, "allow"),
        ("Agent", {"model": "opus"}, None, {}, "deny"),
        ("Agent", {}, None, {}, "allow"),
    ]
    ok = True
    for tool, tool_input, cfg, environ, want in cases:
        got, _ = classify(tool, tool_input, cfg, environ)
        ok = ok and got == want
        print(f"  {'ok  ' if got == want else 'FAIL'} [{got:5}] want={want:5} {tool}: {tool_input}")
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


def _main():
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        sys.exit(0)
    # A non-zero exit is a hook error, which lets the call proceed, so an unknown shape allows on purpose.
    if not isinstance(data, dict):
        sys.exit(0)
    tiers = load_tiers(os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME))
    decision, reason = classify(
        data.get("tool_name"), data.get("tool_input"), tiers, os.environ, data.get("cwd")
    )
    if decision == "deny":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
    sys.exit(0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _main()
