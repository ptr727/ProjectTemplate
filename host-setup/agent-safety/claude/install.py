#!/usr/bin/env python3
"""Install the agent host-safety kit for the current user account. Cross-platform, idempotent.

Deploys the PreToolUse write guard, the PreToolUse model-tier guard with its tier mapping, the
SessionEnd stray-process sweep, and the tool-containment shell prefix. It registers the hooks in the
user settings.json, in exec form so no shell runs them. On a host with a systemd user manager it
also sets the prefix in that file's `env`.
It sets the default subagent model there too, the model of the mapping's default tier.
It merges the permission rules this kit owns into the same file. It merges the `autoMode`
rules this kit owns there too, each found by its tag. It sets the AGENTS.md plugin to read
CLAUDE.md and AGENTS.md both. It sets `attribution.sessionUrl` to false where the host holds no
value there, so a web or Remote Control session adds no session link. It renders the user
CLAUDE.md whole from the kit's blocks and the host-local instruction file. Each hook is self-tested
before it is registered.
The bash and PowerShell wrappers both call this, so every OS runs one tested code path.

Every run records a stamp at ~/.claude/agent-safety-stamp.json naming the machine, what was
installed, and the hub commit it came from.
A fleet rollout can therefore be tracked from the hosts rather than from memory. `--report` reads that stamp against this checkout and answers whether
the machine is current, without changing anything.

Usage: python3 install.py            (installs to ~/.claude).
       python3 install.py --report   (read-only: is this machine current?).
       CLAUDE_HOME=/x python3 install.py   (override target, for testing).
       AGENT_SAFETY_DIRTY_OVERRIDE=0/1 python3 install.py   (force the dirty-checkout signal, for testing).
       AGENT_SAFETY_CONTAINMENT_OVERRIDE=0/1 python3 install.py   (force the containment-capable signal, for testing).
       AGENT_FLEET_LOCAL_INSTRUCTIONS=/x.md python3 install.py   (override the host-local file, for testing).
       AGENT_SAFETY_SOURCE_COMMIT=<40 hex> python3 install.py   (name the commit of a tree git cannot answer for, which the bootstrap sets).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import types
from collections.abc import Callable
from typing import NoReturn

HERE = pathlib.Path(__file__).resolve().parent

# NAME serves both the writer (DEPLOYED_HOOKS, hook_dst/sweep_dst) and the reader (`names_hook`).
# A rename made on one side and not the other reports the registration absent forever.
# STEM is read by the test suite alone now, as a substring convenience over a real command.
GUARD_NAME = "gh-write-guard.py"
GUARD_STEM = "gh-write-guard"
SWEEP_NAME = "stray-process-sweep.py"
SWEEP_STEM = "stray-process-sweep"
CONTAIN_NAME = "tool-containment.py"
TIER_NAME = "model-tier-guard.py"
TIER_CONFIG_NAME = "model-tiers.json"
# The tier guard fires on the tools that dispatch a subagent, so it is its own group and not the Bash one.
TIER_MATCHER = "Agent|Workflow"

PREFIX_VAR = "CLAUDE_CODE_SHELL_PREFIX"

SUBAGENT_VAR = "CLAUDE_CODE_SUBAGENT_MODEL"
SUBAGENT_FORCE_VAR = "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"
STAMP_SUBAGENT_KEY = "subagentModel"

# The hook files this kit copies into ~/.claude/hooks, in deploy order.
# Named here rather than spelled inside `main`, since a test scraping `main` for a literal path goes silent when the copy is refactored.
# That silence reads as a pass.
DEPLOYED_HOOKS = (GUARD_NAME, SWEEP_NAME, CONTAIN_NAME, TIER_NAME)

# The tier mapping lives in the hub's spec/ so a gate and the hook read one file, and is copied here, never edited here.
DEPLOYED_DATA = {TIER_CONFIG_NAME: HERE.parent.parent.parent / "spec" / TIER_CONFIG_NAME}

# A SessionEnd hook's own budget is 1.5 seconds, raised to the highest per-hook timeout the settings declare.
# The sweep's own calls are each bounded, two `ps` reads at 5s and four `systemctl` calls at 5s, all inside this.
SWEEP_TIMEOUT_SECONDS = 35

LAUNCHER_FLOOR = (3, 11)
LAUNCHER_TOKEN = "agent-safety-launcher"
LAUNCHER_PROBE = f"import sys; print({LAUNCHER_TOKEN!r}, *sys.version_info[:2])"
LAUNCHER_TIMEOUT_SECONDS = 30

# The stamp's own format version, separate from the content it describes.
# A reader that predates a field needs to know the shape changed rather than infer it from a missing key.
STAMP_VERSION = 1

# The marker-delimited blocks this kit maintains in CLAUDE.md, in the order they are written and hashed.
# One list rather than the marker pair repeated at each reader.
# A block added to one reader and not the others is installed and then never checked by what reports on it.
CLAUDE_MD_BLOCKS = (
    ("agent-safety", "claude-md-safety.md"),
    ("fleet-bootstrap", "claude-md-fleet.md"),
)
BLOCK_MARKERS = tuple(marker for marker, _ in CLAUDE_MD_BLOCKS)

# The host-local file appended to the rendered instruction file, the one place host-specific text lives.
# One file rather than one per agent, since a host's own notes are rarely about a single agent.
# Another agent's global file is rendered from these same parts, so adding one changes no part of this.
LOCAL_INSTRUCTIONS_ENV = "AGENT_FLEET_LOCAL_INSTRUCTIONS"
LOCAL_MARKER = "host-local"

# The marker the bootstrap writes into a tree it extracted, and the variable it hands that tree's commit in.
# A tarball has no .git to answer for it, so the loader resolves its ref to a commit before downloading and passes it here.
BOOTSTRAP_OWNED_MARKER = ".bootstrap-owned"
BOOTSTRAP_COMMIT_MARKER = ".bootstrap-commit"
SOURCE_COMMIT_ENV = "AGENT_SAFETY_SOURCE_COMMIT"
FULL_COMMIT = re.compile(r"[0-9a-f]{40}")

# The files whose bytes this kit actually places on a machine, the hook first and then each block.
# Derived rather than listed, so a block added above enters the digest without a second edit.
# Written out, this list and the block list drifted apart silently and the digest stopped covering a file.
# The digest is taken over these rather than over the commit, since it is the content that runs.
# A clean commit and a dirty checkout install different bytes while reporting the same SHA.
PAYLOAD_FILES = (
    DEPLOYED_HOOKS + tuple(DEPLOYED_DATA) + tuple(filename for _, filename in CLAUDE_MD_BLOCKS)
)

# Distinguishes an absent key from one holding an explicit null, which `dict.get` reports alike.
# The two need different answers, since a gap is filled and a null is a settings error.
MISSING = object()


def payload_source(name):
    """Where a payload file is read from, the kit directory unless it is a data file kept in spec/."""
    return DEPLOYED_DATA.get(name, HERE / name)


def hook_command(launcher, path):
    """The exec-form `command` and `args` a registered hook carries, spelled once for writer and reader alike.

    Exec form spawns `command` directly with `args` as its argument vector, and no shell is involved.
    A shell-form string runs under `sh -c` on Linux and macOS, and on Windows under Git Bash, or
    PowerShell where Git Bash is absent. So what such a registration ran depended on the host's shell.
    A launcher path two shells split differently could run under one and not the other.
    Exec form also leaves the kit's own hooks outside `CLAUDE_CODE_SHELL_PREFIX`, which Claude Code
    applies to shell-form hooks only.
    Claude Code added exec form in 2.1.139, below the fleet floor of 2.1.281.
    """
    return {"command": str(launcher), "args": [str(path)]}


def exec_form(hook):
    """Whether registration entry `hook` is exec form, which Claude Code selects by the presence of `args`."""
    return "args" in hook


def file_name(word):
    """The last path segment of `word`, read with either slash as the separator."""
    return os.path.basename(str(word).replace("\\", "/"))


def literal_path(word, path):
    """Whether `word`, one argument passed with no shell, is `path` itself, in either slash."""
    return isinstance(word, str) and word.replace("\\", "/") == str(path).replace("\\", "/")


def runs_hook(hook, path):
    """Whether registration entry `hook` runs the hook deployed at `path`.

    An exec-form entry is read from its `command` and `args` directly.
    It runs the hook only when `args` holds exactly the deployed path, the shape this installer writes.
    No shell expands `~` or `$HOME` there, so only the literal path counts.
    A `command` naming the hook itself is not accepted, since exec form cannot spawn a `.py` on Windows.
    A shell-form entry, one an older install or a hand edit left, is read by `shell_runs_hook`.
    """
    if not exec_form(hook):
        return shell_runs_hook(hook.get("command"), path)
    args = hook["args"]
    return isinstance(args, list) and len(args) == 1 and literal_path(args[0], path)


def names_hook(hook, name):
    """Whether registration entry `hook` runs a file whose own name is exactly `name`, at any path.

    It is the ownership test a re-run strips by, so it reads both forms.
    That is what lets a re-run rewrite a shell-form registration in exec form rather than add one beside it.
    An exec-form entry is named when `command` or any string in `args` ends in `name`.
    Each of those words is also read the way `shell_names_hook` reads a whole command.
    So the script string an exec-form `bash -c` carries in its arguments is named too.
    """
    if not exec_form(hook):
        return shell_names_hook(hook.get("command", ""), name)
    args = hook["args"] if isinstance(hook["args"], list) else []
    return any(
        isinstance(word, str) and (file_name(word) == name or shell_names_hook(word, name))
        for word in [hook.get("command"), *args]
    )


def shell_runs_hook(command, path):
    """Whether shell-form `command` names the hook deployed at `path`, as its own argument.

    The path is compared and the launcher is not.
    Matching the name alone let an unrelated `echo stray-process-sweep` count as a registration.
    Matching the whole command string reported a working machine stale.
    That is because `hook_launcher()` resolves at check time and a machine installed under a different PATH carries the other spelling.
    The path is what the installer writes and never drifts, so it is the half worth comparing.

    The quoting around it is not compared either.
    This installer wrote the path in double quotes before it wrote exec form.
    A registration written by hand or through the `/hooks` UI runs the same file bare or in single quotes.
    So requiring one spelling reported those as naming a hook they do not run.

    Whether the command then runs it is not decidable from the string: `echo "<path>"` names the
    deployed hook and executes nothing. What this rules out is the decoy that names the hook and
    not its deployed path, which is the shape a stale registration actually takes.
    """
    text = str(command)
    spellings = {str(path)}
    home = str(pathlib.Path.home())
    if str(path).startswith(home):
        # `~` and `$HOME` are how a person writes this path, and both resolve to the deployed file.
        spellings.add("~" + str(path)[len(home) :])
        spellings.add("$HOME" + str(path)[len(home) :])
    # Forward slashes are accepted on Windows and are the spelling a JSON file usually carries.
    spellings.update(sp.replace("\\", "/") for sp in set(spellings))
    return any(
        re.search(rf"(?:^|[\s\"'(]){re.escape(sp)}(?=$|[\s\"')|&;])", text) for sp in spellings
    )


def shell_names_hook(command, name):
    """Whether shell-form `command` runs a file whose own name is exactly `name` (e.g. GUARD_NAME), at any path.

    Matched with `shell_runs_hook`'s own boundary characters rather than by tokenizing the command.
    A command `shell_runs_hook` already treats as running the deployed hook carries wrapping characters
    attached to the path with no separating space.
    Such a command is, for example, one wrapped in `bash -c "..."`, in `(...)`, or followed by `; true`.
    `shlex.split` folds those characters into the token.
    A plain whitespace split (its own fallback on an unbalanced quote) leaves them attached to a quote character.
    So neither reads the trailing path segment as `name` alone.

    Matched as a trailing path segment rather than a bare substring.
    So a maintainer's own hook whose file name merely contains this kit's stem, such as
    `my-gh-write-guard-audit.sh` containing `gh-write-guard`, is not claimed.
    The character right after the stem there is `-`, not one of the boundary characters this match requires.

    A quoted path is read to its closing quote rather than through the same boundary characters.
    That is because those are literal there: `"/c/Program Files (x86)/hooks/gh-write-guard.py"` is a path
    `shell_runs_hook` accepts whole.
    It is also because the bare-word boundary class below cannot cross the space or the parentheses inside that path to reach the deployed name.

    Known gap, not chased further here:
    - an unquoted path whose own directory holds one of the bare-word boundary characters.
    - a handful of shell compositions `shell_runs_hook` itself only accepts because it is handed the exact
      literal path rather than discovering one.
    The compositions are a backtick, a no-space redirect, and a quote escaped inside an outer quote.
    Closing any of these gaps needs `shell_runs_hook` widened too, which is a design change on its own rather than a local match.
    """
    text = str(command).replace("\\", "/")
    escaped = re.escape(name)
    quoted = rf'"(?:[^"]*/)?{escaped}"' + "|" + rf"'(?:[^']*/)?{escaped}'"
    bare = rf"(?:^|[\s\"'(])(?:[^\s\"'()|&;]*/)?{escaped}(?=$|[\s\"')|&;])"
    return re.search(quoted, text) is not None or re.search(bare, text) is not None


def matcher_sees_tool(matcher, tool):
    """Whether a PreToolUse matcher group receives a call to `tool`.

    An absent or empty matcher runs on every tool and `*` is the all-tools spelling, so neither is
    a defect. A matcher is otherwise a regular expression, which makes `Bash|Task` as good as
    `Bash`. Requiring the exact string reported all three of those working shapes as broken.
    """
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def matcher_covers_every_exit_reason(matcher):
    """Whether a SessionEnd matcher has a shape known to run on every exit reason, absent, empty, or `*`.

    A SessionEnd matcher filters by exit reason the way a PreToolUse matcher filters by tool.
    An absent or empty matcher runs on every reason and `*` is the all-reasons spelling, so none of the three is a defect.
    Unlike `matcher_sees_tool`, there is no single reason to test regex membership against.
    That is because the sweep must fire on every exit reason rather than one particular one.
    So this classifies the shape rather than evaluating a pattern.
    """
    return matcher is None or matcher in ("", "*")


def register_hook(groups, name, entry, selects, matcher=None):
    """Leave `entry` as the only registration of hook `name` across an event's matcher `groups`.

    Every entry naming the hook is stripped first, so a re-run leaves no duplicate behind even
    where more than one group would select it. `entry` then goes into the first group `selects`
    accepts, or into a new group carrying `matcher`, which an absent `matcher` leaves unfiltered.
    """
    for g in groups:
        hooks_list = g.get("hooks")
        if isinstance(hooks_list, list):
            hooks_list[:] = [h for h in hooks_list if not names_hook(h, name)]
    group = next((g for g in groups if selects(g)), None)
    if group is None:
        group = {"hooks": []} if matcher is None else {"matcher": matcher, "hooks": []}
        groups.append(group)
    group.setdefault("hooks", []).append(entry)


def owns(entry, prefix):
    """Whether an allow rule names the script the prefix identifies, rather than a longer path.

    The prefix ends at the script name, so a bare `startswith` also claims `pr_review.py-custom`.
    Dropping that would delete a hand-written rule for a different script.
    What separates the two is the character after the name.
    A rule that invokes this script continues with a rule-syntax delimiter, where a different script
    continues with more of its own path.
    """
    return entry.startswith(prefix) and entry[len(prefix) : len(prefix) + 1] in (":", " ", ")")


def replace_owned(held, owned, current):
    """Drop every entry `owned` claims from `held` in place, append `current`, and return the dropped.

    The permission rules and the autoMode entries share it, so their two strips cannot drift apart.
    """
    dropped = [e for e in held if owned(e)]
    held[:] = [e for e in held if not owned(e)] + current
    return dropped


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
    # The review loop's reply and resolve, the `merge` squash into develop, and the `cleanup` delete of a merged head.
    # Driving the loop by hand needs a raw mutation carrying an id, which is the shape to avoid.
    # The `:*` rule approves any arguments, so `merge` checks the gate's readable items in code, as `cleanup` checks its delete.
    # The rule decides which command skips a prompt, and it bounds no checkout, since it matches the text.
    ("Bash(python3 scripts/pr_review.py", "Bash(python3 scripts/pr_review.py:*)"),
    (
        "Bash(uv run --no-project --script scripts/pr_review.py",
        "Bash(uv run --no-project --script scripts/pr_review.py:*)",
    ),
]

# Claude Code reads AGENTS.md only where no CLAUDE.md, .claude/CLAUDE.md, or CLAUDE.local.md sits at or above the working directory.
# This setting makes it read both, so a stray CLAUDE.local.md cannot silently stop the fleet's rules loading.
# It is read from user, --settings, or managed settings alone and never from a project's, so the host is where it is set.
# Claude Code before 2.1.285 reads the older plugin ID and ignores the newer one, and 2.1.285 on reads either.
# Both carry the one value, so no version from the fleet floor of 2.1.281 up meets a conflict, whichever ID it prefers.
INSTRUCTION_FILES_PLUGINS = ("agents-md@builtin", "cc-plugin-agents-md@builtin")
INSTRUCTION_FILES = "claude-md-and-agents-md"
INSTRUCTION_FILES_SHAPE = (
    ("pluginConfigs", dict),
    *(
        (f"pluginConfigs/{plugin}{tail}", dict)
        for plugin in INSTRUCTION_FILES_PLUGINS
        for tail in ("", "/options")
    ),
)

# Claude Code adds a session link to the commits and pull requests a web or Remote Control session writes unless this key is false.
# Fleet text never carries a session link, so the kit writes false here and no other `attribution` key.
# A value the host already holds is its own, and a re-run leaves it as it stands.
SESSION_URL_PATH = "attribution/sessionUrl"


def session_url_state(data):
    """How settings.json leaves the session link, as `absent`, `off`, or `foreign`, with the held value in words.

    One reader for the install's write and the report's verdict, so the two cannot class a value differently.
    Claude Code reads `attribution: false` as every attribution hidden, and `attribution: true` as the key left out.
    """
    attribution = data.get("attribution", MISSING)
    if attribution is False:
        return (
            "off",
            "attribution is false, which hides every attribution, the session link included",
        )
    if attribution is True:
        return "foreign", "attribution is true, which Claude Code reads as the key left out"
    if attribution is not MISSING and not isinstance(attribution, dict):
        return "foreign", (
            f"attribution holds {type(attribution).__name__}, where Claude Code reads an object or "
            "a boolean"
        )
    held = at(data, SESSION_URL_PATH)
    if held is MISSING:
        return "absent", "attribution.sessionUrl is absent"
    if held is False:
        return "off", "attribution.sessionUrl is false"
    return "foreign", f"attribution.sessionUrl is {held!r}"


# Claude Code reads `autoMode` from user or managed settings alone, so a host-wide block nobody owns can carry one repository's rules.
# The kit owns an entry by this leading tag rather than by its text, so a reworded rule still replaces the version before it.
AUTO_MODE_TAG = "[agent-safety] "
AUTO_MODE_LISTS = ("environment", "allow", "soft_deny", "hard_deny")
AUTO_MODE_DEFAULTS = "$defaults"

# The rules state what each command checks, since CLAUDE.md and the running skill say who authorizes a write.
AUTO_MODE_RULES = {
    "environment": (
        (
            "`scripts/pr_review.py merge` squash-merges a feature pull request into develop only "
            "once it reads the Merge Gate's mechanically readable items as met, and it refuses a "
            "main base. Whether a merge is authorized is decided by the session's CLAUDE.md rules "
            "and the skill that runs it, never by this command."
        ),
        (
            "`scripts/pr_review.py cleanup` removes a merged feature branch. A numbered call "
            "verifies the pull request merged into develop and deletes the remote branch only at "
            "that pull request's head, refusing any branch a ruleset covers. With `--checkout` it "
            "also removes the branch's worktrees and local branch and fast-forwards develop in "
            "that clone. `--sweep --apply` "
            "removes the local branches and worktrees whose work a merged pull request or origin's "
            "develop or main already holds, and the remote branch of a merged develop pull request. "
            "Neither removes develop or main. Whether a delete is authorized is decided by the "
            "session's CLAUDE.md rules and the skill that runs it, never by this command."
        ),
    ),
    "allow": (
        (
            "Running `scripts/pr_review.py merge` or `scripts/pr_review.py cleanup`, under "
            "`python3` or `uv run --no-project --script`, where a skill the session follows "
            "prescribes it, since each checks its own preconditions in code."
        ),
    ),
}


def tagged(entry):
    """Whether an autoMode entry is this kit's own."""
    return isinstance(entry, str) and entry.startswith(AUTO_MODE_TAG)


def owned_auto_mode():
    """The entries this kit writes into each managed autoMode list, in list order."""
    return {
        name: [AUTO_MODE_TAG + rule for rule in AUTO_MODE_RULES.get(name, ())]
        for name in AUTO_MODE_LISTS
    }


def tagged_auto_mode(data):
    """The tagged entries each managed autoMode list in `data` holds, reading a bad shape as none."""
    auto = data.get("autoMode") if isinstance(data, dict) else None
    out = {}
    for name in AUTO_MODE_LISTS:
        held = auto.get(name) if isinstance(auto, dict) else None
        out[name] = [e for e in held if tagged(e)] if isinstance(held, list) else []
    return out


def defaults_first(held):
    """Whether `held` starts with the built-in splice and carries it exactly once."""
    return held[:1] == [AUTO_MODE_DEFAULTS] and held.count(AUTO_MODE_DEFAULTS) == 1


def defaults_reason(held):
    """Why a list failing `defaults_first` fails, worded for what it is.

    Claude Code splices its built-ins in wherever `$defaults` sits, so only a list with none replaces them.
    A misplaced or repeated one keeps them and breaks only this kit's own rule.
    """
    if AUTO_MODE_DEFAULTS not in held:
        return f"has no {AUTO_MODE_DEFAULTS}, so it replaces Claude Code's built-in rules"
    return f"has {AUTO_MODE_DEFAULTS} but not first, exactly once, as this kit requires"


def put_defaults_first(held):
    """Make `$defaults` the first entry of `held` in place, once, and say what changed (or "" where nothing did).

    A list without the splice replaces Claude Code's built-in rules, which for a deny list drops its built-in denials.
    Every other entry keeps its exact text and its relative order.
    """
    if defaults_first(held):
        return ""
    count = held.count(AUTO_MODE_DEFAULTS)
    held[:] = [AUTO_MODE_DEFAULTS, *(e for e in held if e != AUTO_MODE_DEFAULTS)]
    if count == 0:
        return "added first"
    if count == 1:
        return "moved first"
    return f"kept once, first, with {count - 1} duplicate{'s' if count > 2 else ''} removed"


def auto_mode_bytes(lists):
    """The digest input for a set of autoMode lists, identical on the payload and installed sides."""
    return json.dumps([lists.get(name, []) for name in AUTO_MODE_LISTS]).encode("utf-8")


def auto_mode_misshape(data):
    """The first autoMode key the merge cannot read, as (where, held, want), or None.

    One reader for the install's refusal and the report's verdict, so the two name the same key.
    """
    auto = data.get("autoMode", MISSING)
    if auto is MISSING:
        return None
    if not isinstance(auto, dict):
        return ("autoMode", auto, dict)
    for name in AUTO_MODE_LISTS:
        held = auto.get(name, MISSING)
        if held is MISSING:
            continue
        if not isinstance(held, list):
            return (f"autoMode.{name}", held, list)
    return None


def read_settings(claude_home):
    """settings.json parsed, or None where it is absent, not a regular file, or not readable JSON."""
    try:
        raw = read_regular_file(claude_home / "settings.json")
        return None if raw is None else json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, OSError):
        return None


def containment_capable(prefix, env=None, system=None, uid=None, controllers=None):
    """(capable, reason): whether this host can run the deployed `prefix` and start a scope for a command.

    Judged at install and at report time alike.
    A host that gains or loses its user manager is therefore
    reported against what it can do now. AGENT_SAFETY_CONTAINMENT_OVERRIDE, read as exactly "0" or
    "1", replaces the judgment for a test.
    AGENT_SAFETY_DIRTY_OVERRIDE does the same for the dirty signal.
    """
    env = os.environ if env is None else env
    system = sys.platform if system is None else system
    override = env.get("AGENT_SAFETY_CONTAINMENT_OVERRIDE")
    if override in ("0", "1"):
        return override == "1", "forced by AGENT_SAFETY_CONTAINMENT_OVERRIDE"
    if not system.startswith("linux"):
        return False, f"{system} has no systemd user manager"
    if not shutil.which("systemd-run"):
        return False, "systemd-run is not installed"
    # WSLg, `su -`, cron, and `docker exec` shells point elsewhere or nowhere, while the manager listens here.
    uid = os.getuid() if uid is None else uid
    if not any(
        runtime and os.path.exists(os.path.join(runtime, "systemd", "private"))
        for runtime in (env.get("XDG_RUNTIME_DIR"), f"/run/user/{uid}")
    ):
        return False, "no systemd user manager is running for this account"
    # A scope on a cgroup-v1 or hybrid host, or under a manager not delegated these two, enforces neither ceiling.
    controllers = controllers or (
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
    )
    try:
        with open(controllers, encoding="utf-8") as f:
            delegated = set(f.read().split())
    except OSError:
        delegated = set()
    if not {"pids", "memory"} <= delegated:
        return False, "the user manager is not delegated the pids and memory controllers"
    ran = prefix_runs_directly(prefix)
    if ran:
        return False, f"the deployed prefix does not run as its own executable ({ran})"
    return True, "a systemd user manager is running and the prefix runs"


def prefix_runs_directly(prefix):
    """Why `prefix --selftest` fails when executed the way Claude Code executes it, or "" where it passes.

    Claude Code runs the file itself, through its `env python3` shebang, for every Bash tool call and
    every shell-form hook. The kit's own hooks are exec form, which Claude Code spawns without the
    prefix. So a broken prefix does not reach the write guard. It does reach a shell-form PreToolUse
    hook the host keeps of its own. A failure there is a non-blocking error that lets the call go ahead.
    A prefix can fail to run that way from a missing `python3`, a lost execute bit, or a `noexec` mount.
    """
    try:
        done = subprocess.run(
            [str(prefix), "--selftest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if done.returncode != 0:
        last = (done.stdout + done.stderr).strip().splitlines()[-1:] or ["no output"]
        return f"exit {done.returncode}: {last[0]}"
    return ""


def prefix_value(path):
    """The `CLAUDE_CODE_SHELL_PREFIX` value naming the deployed prefix, spelled once for writer and reader.

    A bare path, since Claude Code quotes the whole value as one word.
    An interpreter named in front of it is therefore read as part of the file name.
    """
    return str(path)


def names_prefix(held, path):
    """Whether a `CLAUDE_CODE_SHELL_PREFIX` value names the deployed prefix."""
    return held == prefix_value(path)


def kit_prefix(held):
    """Whether a `CLAUDE_CODE_SHELL_PREFIX` value names this kit's prefix, deployed here or at another path.

    Judged on the file name exactly, since a substring match claims someone else's wrapper whose name
    merely contains this one's.
    """
    return file_name(held) == CONTAIN_NAME


def foreign_prefix(data):
    """The `CLAUDE_CODE_SHELL_PREFIX` value when it names something other than this kit's prefix, else None."""
    env = data.get("env") if isinstance(data, dict) else None
    held = env.get(PREFIX_VAR) if isinstance(env, dict) else None
    return held if held is not None and not kit_prefix(held) else None


def launcher_problem(launcher):
    """Why `launcher` cannot run a hook, or None when running it prints Python 3.11 or newer.

    The candidate is run and its output read, with its stderr kept for the reason.
    A stub found on PATH or exiting 0 prints neither the token nor a version, so it is refused.
    A Windows app execution alias is one such stub.
    The token over a version that is not two integers is refused too, rather than raised.
    `--report` runs each registered launcher through this too.
    The path a registration names never drifts and its launcher can, so a host installed before this
    probe existed is caught there.
    """
    try:
        r = subprocess.run(
            [launcher, "-c", LAUNCHER_PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LAUNCHER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return f"{launcher!r} could not be run ({e})"
    words = r.stdout.split()
    if r.returncode or len(words) != 3 or words[0] != LAUNCHER_TOKEN:
        detail = (r.stderr.strip() or r.stdout.strip() or "no output")[:200]
        return f"{launcher!r} did not run the version probe (exit {r.returncode}: {detail})"
    try:
        version = (int(words[1]), int(words[2]))
    except ValueError:
        return f"{launcher!r} printed a malformed version probe ({r.stdout.strip()[:200]!r})"
    if version < LAUNCHER_FLOOR:
        return f"{launcher!r} runs Python {version[0]}.{version[1]}, below the 3.11 floor"
    return None


def base_interpreters(path):
    """The interpreters the virtual environment holding `path` was made from, or `[path]` where it is not in one.

    A venv's interpreter goes when the venv does, and a worktree removed with its venv takes it along.
    A hook registered under it would then fail to spawn, a non-blocking error that fails the guard open.
    So the venv's `pyvenv.cfg` is read for the base, each existing one listed in order.
    The first is `python3`, or `python.exe` on Windows, in its `home` directory.
    That is the name a minor-version upgrade keeps working.
    The second is its `executable`, which Python 3.11 and later record.
    That covers a `home` whose `python3` is an older interpreter or absent, as an altinstall leaves it.
    The venv's own path stays the last resort, tried where no base exists or none passes the probe.
    """
    exe = pathlib.Path(path)
    for cfg in (exe.parent / "pyvenv.cfg", exe.parent.parent / "pyvenv.cfg"):
        try:
            lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        keys = {}
        for line in lines:
            key, sep, value = line.partition("=")
            if sep:
                keys[key.strip()] = value.strip()
        named = []
        if keys.get("home"):
            named.append(
                pathlib.Path(keys["home"]) / ("python.exe" if os.name == "nt" else "python3")
            )
        if keys.get("executable"):
            named.append(pathlib.Path(keys["executable"]))
        return [str(b) for b in named if b.is_file()] + [path]
    return [path]


def exec_spawn_problem(launcher, windows=None):
    """Why exec form cannot spawn `launcher`, or None where it can.

    On Windows exec form needs `command` to resolve to a real `.exe`.
    A `.bat` or `.cmd` shim, such as the ones pyenv-win puts on PATH, runs only through a shell.
    Python's own subprocess runs such a shim through `cmd`, so the version probe alone passes it.
    """
    if not (os.name == "nt" if windows is None else windows):
        return None
    resolved = shutil.which(launcher) or launcher
    if pathlib.PurePath(resolved).suffix.lower() != ".exe":
        return f"{launcher!r} resolves to {resolved!r}, not a .exe, which exec form cannot spawn on Windows"
    return None


def hook_launcher():
    """The interpreter path the settings.json entries carry as `command`, or None, and why each other candidate failed.

    The path is absolute, so the hook runs the interpreter the install probed.
    Exec form searches PATH for a bare name, and a session an IDE launched may carry a PATH other than the installing shell's.
    The `python3` on PATH is preferred, being unambiguously Python 3.
    A candidate inside a virtual environment yields first to the interpreters that venv was made from.
    Its path is kept as PATH resolves it, and its links are not followed.
    So `/usr/bin/python3` stays the name a minor-version upgrade keeps pointing at a working interpreter.
    Else this interpreter's own path is used, which an upgrade or a move can break.
    Never a bare `python`, which is Python 2 on some systems and would fail the hook's Python 3 syntax.
    Each candidate is run before it is chosen, since a hook naming one that never runs fails open.
    `--report` runs the registered one again, so a launcher an upgrade broke reports STALE.
    The install picks it before writing anything and runs each hook's self-test under it, so a pass
    also proves the launcher runs the hook.
    """
    rejected = []
    seen = set()
    for found in (shutil.which("python3"), sys.executable):
        if not found:
            continue
        for candidate in base_interpreters(os.path.abspath(found)):
            if candidate in seen:
                continue
            seen.add(candidate)
            problem = exec_spawn_problem(candidate) or launcher_problem(candidate)
            if problem is None:
                return candidate, rejected
            rejected.append(problem)
    return None, rejected


def registered_launcher(hook, path):
    """The interpreter registration entry `hook` names in front of the hook at `path`, or None where it names none.

    An exec-form entry is read from `command` and `args` directly, with no split, since no shell
    splits them when Claude Code spawns it. Its launcher is `command` exactly as written, a UNC
    path or one holding spaces included. `~` and `$HOME` are not expanded, since nothing expands
    them there. It names a launcher only in the `args` = `[<path>]` shape this installer writes.
    A shell-form entry is one an older install or a hand edit left.
    It is read from the `<launcher> <path>` shape that install wrote, split as POSIX `sh` and Git Bash split it.
    Any other shape is not run, since what it executes is not decidable from the entry.
    """
    if exec_form(hook):
        launcher = hook.get("command")
        if not isinstance(launcher, str) or not launcher:
            return None
        return launcher if runs_hook(hook, path) else None
    try:
        words = shlex.split(str(hook.get("command")))
    except ValueError:
        return None
    if len(words) != 2 or not shell_runs_hook(words[1], path):
        return None
    return os.path.expandvars(os.path.expanduser(words[0]))


def host_facts():
    """Name and kind of this machine, enough to tell one host in the fleet from another.

    The distro is read from /etc/os-release rather than from `platform`, which reports the kernel
    and cannot tell Debian from Ubuntu. WSL is named because it is a distinct rollout target that
    otherwise reports as the Linux it runs.
    """
    facts = {
        "hostname": socket.gethostname(),
        "system": platform.system(),
        "release": platform.release(),
    }
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

    A dirty tree is reported rather than hidden.
    An untracked payload file counts whatever status.showUntrackedFiles says, since its bytes are installed too.
    Each payload path is given relative to the kit, since an absolute pathspec under `git -C` can match nothing and read clean.
    The SHA still names a commit, but the bytes installed are not that commit's.
    A stamp that claims otherwise is the thing this exists to prevent. A checkout that is not a git tree at all (an extracted tarball) says so.

    A tree the bootstrap extracted is asked nothing of git, which would answer for whatever repository encloses it.
    The commit it resolved arrives in AGENT_SAFETY_SOURCE_COMMIT and is recorded as an archive, clean by construction.
    Only a full 40-character lowercase hex commit is honored, so a stray or truncated value is never recorded as a commit.
    A git tree ignores the variable.

    AGENT_SAFETY_DIRTY_OVERRIDE, read as exactly "0" or "1", replaces the git-derived dirty
    signal with a fixed one, and honoring it prints a warning below. Any other value, unset
    included, falls through to the git status read below, so a stray or misspelled setting
    cannot silently force a verdict. This exists so a test can assert a verdict without
    depending on whether this very kit happens to be mid-edit while the suite runs. It never
    changes what a real, unset-env install reports.
    """

    def git(*args):
        # A host with no git is the normal case for a tarball install, and it is not an error here.
        # Letting FileNotFoundError escape would crash both the install and the read-only report.
        try:
            r = subprocess.run(
                ["git", "-C", str(HERE), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except OSError:
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    # Resolves an archive commit as scripts/skills_install.py source_ref does: the handed variable first, then the .bootstrap-commit marker.
    root = HERE.parent.parent.parent
    extracted = (root / BOOTSTRAP_OWNED_MARKER).is_file()
    sha = None if extracted else git("rev-parse", "HEAD")
    if not sha:
        handed = os.environ.get(SOURCE_COMMIT_ENV, "")
        if not FULL_COMMIT.fullmatch(handed):
            try:
                handed = (root / BOOTSTRAP_COMMIT_MARKER).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                handed = ""
            # A malformed marker is no commit, so it falls through to the commit-less archive record.
            if not FULL_COMMIT.fullmatch(handed):
                handed = ""
        if handed:
            return {"vcs": "archive", "commit": handed, "dirty": False}
        # A bootstrap tree whose loader resolved no commit is still an archive.
        if extracted:
            return {"vcs": "archive", "commit": None, "dirty": False}
        return {"vcs": "none"}
    ref = {"vcs": "git", "commit": sha}
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        ref["branch"] = branch
    override = os.environ.get("AGENT_SAFETY_DIRTY_OVERRIDE")
    if override in ("0", "1"):
        ref["dirty"] = override == "1"
        print(
            f"AGENT_SAFETY_DIRTY_OVERRIDE={override} is forcing the dirty-checkout signal. "
            "The real git status was not read.",
            file=sys.stderr,
        )
    else:
        status = git(
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *(
                pathlib.Path(os.path.relpath(payload_source(n), HERE)).as_posix()
                for n in PAYLOAD_FILES
            ),
        )
        ref["dirty"] = bool(status)
    return ref


def normalized(data):
    """Line endings reduced to newlines, covering CRLF and a bare CR.

    One helper rather than a replace at each site. Both digests have to normalize identically or a
    machine drifts on nothing.
    A site that handled CRLF while missing CR did exactly that.
    The installer reads a snippet in text mode, so a bare CR arrives as a newline and installs as one.
    But a digest that left it alone reported the machine STALE against its own content.
    """
    if isinstance(data, bytes):
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data.replace("\r\n", "\n").replace("\r", "\n")


def local_instructions_path():
    """The host-local instruction file, under the XDG config root unless the override names one."""
    override = os.environ.get(LOCAL_INSTRUCTIONS_ENV)
    if override:
        # Absolute rather than resolved, so a symlink stays a symlink and a dangling one is still seen.
        return pathlib.Path(override).expanduser().absolute()
    # The XDG spec treats a relative value as unset, and honoring one made the file depend on the cwd.
    root = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or "").expanduser()
    base = root if root.is_absolute() else pathlib.Path.home() / ".config"
    return base / "agent-fleet" / "local.md"


def read_regular_file(path):
    """The bytes of `path` where it is a regular file, or None where it is anything else.

    Raises OSError where the open itself fails, FileNotFoundError included for a dangling link.
    """
    fd = _open_regular_file(path, os.O_RDONLY)
    if fd is None:
        return None
    # Closed in a finally, so no return or raise after the open leaves the descriptor behind.
    try:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _open_regular_file(path, access):
    """A descriptor open on `path` with `access`, or None, closed, where it is not a regular file.

    Opened without blocking and judged on the open descriptor, so a FIFO or a device returns None.
    It neither hangs a read nor swallows a write.
    Judging on the open descriptor also means nothing can swap the path between the check and the use.
    Raises OSError where the open itself fails.
    """
    flags = access | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    # Windows refuses to open a directory at all, and a directory is still not a regular file.
    except FileNotFoundError:
        raise
    except OSError:
        if os.path.isdir(path):
            return None
        raise
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        return None
    return fd


class NotRegularFile(OSError):
    """A write target that exists and is not a regular file, refused before anything was written."""


def write_regular_file(
    path: str | os.PathLike[str], data: bytes, before: Callable[[], None] | None = None
) -> None:
    """Replace `path`, a regular file or absent, with a file holding `data`.

    The data goes to a temporary file beside the target, and `os.replace` then swaps it in whole.
    So a failure or an interrupt leaves the target as it was found, holding its prior content or absent.
    A path that is a link is resolved, and the file it points at is replaced, so a dotfiles symlink stays a link.

    The target is opened for writing first, without truncating it, and judged on that descriptor.
    So a file this run cannot write is refused before anything is made.
    A FIFO or a device is refused rather than hanging or swallowing the write.
    A replaced file keeps its permission bits.
    `before`, which backs the file up, runs once the new content is whole on disk, just before the swap.
    So a write refused or cut short leaves no backup behind.

    The temporary file is removed on any failure, an interrupt included, where its path still holds the file this call created.
    Raises OSError with the target unchanged, naming any temporary file it could not remove.
    """
    target = pathlib.Path(os.path.realpath(path))
    mode = _writable_mode(path, target)
    temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    made = None
    try:
        try:
            made = os.fstat(fd)
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            _write_all(fd, data)
            os.fsync(fd)
        except BaseException:
            try:
                os.close(fd)
            # The write's own error is the one raised, and the temporary file is removed below.
            except OSError:
                pass
            raise
        # A network filesystem can report a quota or I/O error at the close rather than at the write.
        os.close(fd)
        if before is not None:
            before()
        os.replace(temp, target)
    except BaseException as e:
        try:
            _remove_if_same_file(temp, made)
        except OSError as unlink_error:
            if isinstance(e, Exception):
                raise OSError(
                    f"{e}, and the temporary file {temp} could not be removed ({unlink_error})"
                ) from e
        raise


def _writable_mode(path, target):
    """The permission bits of `target` where it is a regular file this run can write, or None where it is absent."""
    try:
        fd = _open_regular_file(target, os.O_WRONLY)
    except FileNotFoundError:
        return None
    if fd is None:
        raise NotRegularFile(f"{path} is not a regular file")
    try:
        return stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def _remove_if_same_file(path, made):
    """Remove `path` where it is still the file `made` describes, and raise OSError where it is not.

    Compared rather than removed by name, since something else may have put its own file at the
    path once the descriptor closed. The comparison narrows the time in which such a file could be
    removed to the moment between it and the removal, rather than closing it. A path already gone
    is left gone.
    """
    if made is None:
        raise OSError(f"{path} could not be identified as the file this run created")
    try:
        now = os.lstat(path)
    except FileNotFoundError:
        return
    if (now.st_dev, now.st_ino) != (made.st_dev, made.st_ino):
        raise OSError(f"{path} is no longer the file this run created")
    os.unlink(path)


def _write_all(fd, data):
    """Write all of `data` to the open file, since one write may take only part of it."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def read_local_instructions(local_path):
    """The host-local text, stripped, and why it cannot be used, or None where it can.

    Checked before anything is installed, so an unusable file stops the run with nothing changed.
    A file carrying this kit's own markers is refused, since appending it would duplicate a block.
    Copying an old backup into the local file is exactly how that happens.
    """
    # The probe is lstat rather than exists(), which reads a dangling symlink as absent.
    # From Python 3.14, exists() also reads a parent directory that cannot be entered as absent rather than raising.
    try:
        info = os.lstat(local_path)
    # Only a missing path is absent, so a parent that is a file is refused rather than silently skipped.
    # Windows raises FileNotFoundError for a file parent too, so the nearest existing ancestor decides.
    except FileNotFoundError:
        for parent in local_path.parents:
            try:
                parent_info = os.stat(parent)
            except FileNotFoundError:
                continue
            except OSError as e:
                return "", f"{local_path} cannot be read ({e})"
            if not stat.S_ISDIR(parent_info.st_mode):
                return "", f"{local_path} cannot be read, since {parent} is not a directory"
            break
        return "", None
    except OSError as e:
        return "", f"{local_path} cannot be read ({e})"
    try:
        raw = read_regular_file(local_path)
    except FileNotFoundError:
        gone = (
            "is a link that leads to nothing" if stat.S_ISLNK(info.st_mode) else "no longer exists"
        )
        return "", f"{local_path} {gone}"
    except OSError as e:
        return "", f"{local_path} cannot be read ({e})"
    if raw is None:
        return "", f"{local_path} is not a regular file"
    try:
        text = normalized(raw.decode("utf-8")).strip()
    except UnicodeDecodeError as e:
        return "", f"{local_path} is not UTF-8 ({e})"
    for marker in (*BLOCK_MARKERS, LOCAL_MARKER):
        if re.search(rf"<!-- {marker} (?:v\d+ )?(?:start|end) -->", text):
            return "", (
                f"{local_path} carries this kit's {marker} marker, so appending it would duplicate "
                "kit content. Keep only host-specific text in it"
            )
    return text, None


def render_instructions(local_path, local_text, header=None):
    """The whole global instruction file in newline form: a header, each block, then the local file.

    A caller writing the same content for another agent passes its own header, which is the only part that differs.

    Rendered whole rather than merged into whatever the file held. Hand-written sections outside the
    blocks were never written or checked, so they restated rules in wording the fleet had since changed.
    Every session on the host read them anyway.
    """
    if header is None:
        header = (
            "<!-- Written by ProjectTemplate host-setup/agent-safety, which rewrites this whole file on "
            f"every install. Put host-specific content in {local_path}, appended below. -->"
        )
    parts = [header]
    parts += [
        (HERE / filename).read_text(encoding="utf-8").strip() for _, filename in CLAUDE_MD_BLOCKS
    ]
    if local_text:
        parts.append(f"<!-- {LOCAL_MARKER} start -->\n{local_text}\n<!-- {LOCAL_MARKER} end -->")
    return normalized("\n\n".join(parts)) + "\n"


def text_digest(text):
    """A digest over text with its line endings normalized, so CRLF and LF copies agree."""
    return hashlib.sha256(normalized(text).encode("utf-8")).hexdigest()[:16]


def trusted_stamp(claude_home):
    """The stamp as an object, or None where it is missing, unreadable, or fails its shape check.

    A stamp failing its own shape check vouches for nothing, so no field of it is trusted either.
    """
    try:
        raw = read_regular_file(claude_home / "agent-safety-stamp.json")
        stamp = None if raw is None else json.loads(raw.decode("utf-8"))
    except (ValueError, OSError):
        return None
    if stamp is None or stamp_problems(stamp):
        return None
    return stamp


def stamped_instructions_digest(claude_home):
    """The digest of the instruction file the installer last wrote or left, or None where none is.

    None covers a missing or unreadable stamp and one written before the field existed. Each of
    those leaves an edit indistinguishable from an earlier render, so a caller treats it as an edit.
    """
    stamp = trusted_stamp(claude_home)
    value = stamp.get("instructionsDigest") if stamp is not None else None
    return value if isinstance(value, str) else None


def stamped_subagent_model(claude_home):
    """The default subagent model this kit last recorded writing, or None where it recorded none.

    None covers a stamp written before the field existed, so a value already on such a host is the host's own.
    """
    stamp = trusted_stamp(claude_home)
    value = stamp.get(STAMP_SUBAGENT_KEY) if stamp is not None else None
    return value if isinstance(value, str) else None


def tier_defaults():
    """The default tier and its model, and the maintainer-only tier and its model, or None where unreadable.

    Read from the mapping this checkout deploys, through the tier guard's own reader.
    So the installer and the hook never read one mapping two ways.
    """
    spec = importlib.util.spec_from_file_location("model_tier_guard", HERE / TIER_NAME)
    if spec is None or spec.loader is None:
        return None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    tiers = guard.load_tiers(payload_source(TIER_CONFIG_NAME))
    if tiers is None:
        return None
    return {
        "default_tier": tiers["default_tier"],
        "default": tiers["default"],
        "reserved_tier": tiers["reserved"],
        "reserved": tiers["blocked"],
    }


def subagent_force(env_block, environ=None):
    """Where `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is set, said in words, or None where it is not."""
    environ = os.environ if environ is None else environ
    held = env_block.get(SUBAGENT_FORCE_VAR) if isinstance(env_block, dict) else None
    if held is not None and held != "":
        return f"settings.json sets {SUBAGENT_FORCE_VAR} to {held!r}"
    if environ.get(SUBAGENT_FORCE_VAR):
        return (
            f"this shell's environment sets {SUBAGENT_FORCE_VAR} to {environ[SUBAGENT_FORCE_VAR]!r}"
        )
    return None


def subagent_model_state(held, recorded, default):
    """How the kit treats a held `CLAUDE_CODE_SUBAGENT_MODEL`, as one of four states.

    "absent" is no key, `held` being MISSING, or an empty or null value, which sets no default either.
    "current" is the default itself. "outdated" is the value this kit last recorded writing, where
    the default has since changed. "foreign" is anything else, which is the host's own and is never
    overwritten.
    """
    if held is MISSING or held is None or held == "":
        return "absent"
    if held == default:
        return "current"
    if isinstance(recorded, str) and held == recorded:
        return "outdated"
    return "foreign"


def foreign_subagent_note(held, tiers):
    """What a host's own `CLAUDE_CODE_SUBAGENT_MODEL` means, naming the maintainer-only tier where it is that one."""
    note = (
        f"{SUBAGENT_VAR} is {held!r}, which this kit does not own, so a dispatch naming no model "
        f"runs there rather than at {tiers['default']!r}, the {tiers['default_tier']} tier's model."
    )
    if isinstance(held, str) and held.lower() == tiers["reserved"].lower():
        note += (
            f" That is the {tiers['reserved_tier']} tier's model, so the default dispatch tier on "
            "this host is the maintainer-only one."
        )
    return note


def subagent_model_findings(env_block, claude_home):
    """(problems, notes) about the default subagent model, a problem being what a re-run of the installer clears."""
    tiers = tier_defaults()
    if tiers is None:
        return [
            f"{TIER_CONFIG_NAME} in this checkout cannot be read, so {SUBAGENT_VAR} cannot be checked"
        ], []
    held = env_block.get(SUBAGENT_VAR, MISSING) if isinstance(env_block, dict) else MISSING
    forced = subagent_force(env_block)
    if forced:
        return [], [
            (
                f"{forced}, which flattens every subagent onto one model whatever tier a dispatch "
                f"names. This kit leaves it, and {SUBAGENT_VAR}, as they are."
            )
        ]
    state = subagent_model_state(held, stamped_subagent_model(claude_home), tiers["default"])
    if state == "absent":
        return [
            (
                f"{SUBAGENT_VAR} is not set, so a dispatch naming no model runs at the session's "
                f"own model rather than at {tiers['default']!r}, the {tiers['default_tier']} tier's model."
            )
        ], []
    if state == "outdated":
        return [
            (
                f"{SUBAGENT_VAR} is {held!r}, which this kit wrote, where the "
                f"{tiers['default_tier']} tier's model is now {tiers['default']!r}"
            )
        ], []
    if state == "foreign":
        return [], [foreign_subagent_note(held, tiers)]
    return [], []


def write_backup(claude_md, raw):
    """Write `raw` to a new backup beside CLAUDE.md and return its path, never replacing an earlier one.

    Created exclusively rather than checked and then written.
    So two runs in one second cannot both pick the same free name and have the second overwrite the first.
    """
    when = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    n = 0
    while True:
        suffix = f"{when}.bak" if n == 0 else f"{when}-{n}.bak"
        candidate = claude_md.with_name(f"{claude_md.name}.{suffix}")
        try:
            with open(candidate, "xb") as f:
                f.write(raw)
            return candidate
        except FileExistsError:
            n += 1


def payload_digest():
    """One digest over the content this kit installs, normalized the way the installer writes it.

    Over raw bytes this reported drift a reinstall could not clear.
    The snippets are embedded with `.strip()`, so a trailing newline moved the digest while the installed block stayed identical.
    The machine was therefore told to re-run something that would write the same file.
    Line endings normalize for the same reason.

    Fixed order because a set of files has none.
    It is also fixed because a digest that depends on directory listing
    order reports drift on a machine where nothing changed. The order matches the one
    `installed_digest` reads, so the two are directly comparable: each deployed hook, then each data
    file, then each block.
    """
    h = hashlib.sha256()
    for name in PAYLOAD_FILES:
        raw = normalized(payload_source(name).read_bytes())
        # A snippet is embedded stripped, so trailing whitespace is not installed content.
        # The hook is copied byte for byte, so nothing about it is stripped.
        if name.endswith(".md"):
            raw = raw.decode("utf-8").strip().encode("utf-8")
        h.update(raw)
    h.update(auto_mode_bytes(owned_auto_mode()))
    return h.hexdigest()[:16]


def read_claude_md(claude_md):
    """CLAUDE.md's text, or None where it is absent, not a regular file, or unreadable.

    One reader for every caller.
    So a file none of them can read gives each the same answer rather
    than a traceback in whichever reads it first.
    """
    try:
        raw = read_regular_file(claude_md)
    except OSError:
        return None
    return None if raw is None else raw.decode("utf-8", errors="replace")


def blocks_present(claude_md):
    """The marker version of each block actually in CLAUDE.md, by name.

    Read from the file rather than from what the installer meant to write, since the question the
    stamp answers is what is on the machine.
    """
    text = read_claude_md(claude_md)
    if text is None:
        return {}
    found = {}
    for marker in BLOCK_MARKERS:
        # A start marker alone is a half-written block, which a presence check reads as installed.
        # Exactly one pair, since the installer writes one and a duplicate is a corrupted file.
        # Two blocks mean the second silently governs, and reporting the first as current hides that.
        starts = re.findall(rf"<!-- {marker} (v\d+) start -->", text)
        ends = re.findall(rf"<!-- {marker} (v\d+) end -->", text)
        if len(starts) == 1 and starts == ends:
            found[marker] = starts[0]
    return found


def marker_corruption(claude_md):
    """Markers present in the file that yield no valid block, meaning duplicated or half-written.

    Judged against the file alone, never against the stamp. An install onto an already-corrupted
    CLAUDE.md records the same empty block set it reads.
    So the stamp and the file agree and the
    corruption reads as a match. Two wrong answers agreeing is the failure this exists to catch.
    """
    text = read_claude_md(claude_md)
    if text is None:
        return []
    valid = blocks_present(claude_md)
    out = []
    for marker in BLOCK_MARKERS:
        if re.search(rf"<!-- {marker} v\d+ (?:start|end) -->", text) and marker not in valid:
            out.append(f"the {marker} markers in CLAUDE.md are duplicated or incomplete")
    return out


def installed_digest(claude_home):
    """A digest over the bytes actually on this machine, or None where the kit is not fully there.

    Markers and versions answer whether a block is present, and nothing about its content.
    So a block edited between its own markers reports current under a presence check. The hook is not
    marker-delimited at all, so a modified or deleted one is invisible the same way.

    Line endings are normalized first: CLAUDE.md keeps whatever endings it had, and a machine that
    holds identical text with CRLF is current rather than drifted.
    """
    # Read from `DEPLOYED_HOOKS` rather than by name, since naming them here is the drift that constant closes.
    # A hook added to the deploy list and not to this one installs and is never covered by the currentness digest.
    deployed = [claude_home / "hooks" / name for name in (*DEPLOYED_HOOKS, *DEPLOYED_DATA)]
    claude_md = claude_home / "CLAUDE.md"
    claude_text = read_claude_md(claude_md)
    try:
        hooks = [read_regular_file(f) for f in deployed]
    except OSError:
        return None
    if None in hooks or claude_text is None:
        return None
    h = hashlib.sha256()
    for raw in hooks:
        h.update(normalized(raw))
    text = normalized(claude_text)
    for marker in BLOCK_MARKERS:
        found = re.search(
            rf"<!-- {marker} v\d+ start -->.*?<!-- {marker} v\d+ end -->", text, re.DOTALL
        )
        if not found:
            return None
        h.update(found.group(0).encode("utf-8"))
    h.update(auto_mode_bytes(tagged_auto_mode(read_settings(claude_home))))
    return h.hexdigest()[:16]


def build_stamp(claude_home, installed, instructions_digest=None, subagent_model=None):
    """The record written to the machine after an install, or computed live for a report.

    An install passes the digest of what it rendered rather than re-reading the file. A write by
    anything else between the install's write and this read would otherwise be stamped as the install's own.
    The next run would otherwise also replace it with no backup.
    `subagent_model` is the default subagent model this kit wrote and still owns, or None where it owns none.
    """
    return {
        "stampVersion": STAMP_VERSION,
        "host": host_facts(),
        "source": source_ref(),
        "payloadDigest": payload_digest(),
        "installedDigest": installed_digest(claude_home),
        "blocks": blocks_present(claude_home / "CLAUDE.md"),
        "instructionsDigest": instructions_digest
        or (
            text_digest(claude_text)
            if (claude_text := read_claude_md(claude_home / "CLAUDE.md")) is not None
            else None
        ),
        "installedUtc": installed,
        STAMP_SUBAGENT_KEY: subagent_model,
    }


# Checked before a stamp is read, so a hand-edited or older-format file gives a verdict rather than a traceback.
# Shape rather than presence: a partial write leaves keys missing, and a hand edit leaves a key holding the wrong type.
# A key check alone passes `"source": "git"` and then raises inside the line that formats it, which is the crash it was added to prevent.
STAMP_SHAPE = {
    "stampVersion": int,
    "host": dict,
    "source": dict,
    "payloadDigest": str,
    "blocks": dict,
    "installedUtc": str,
}


def stamp_problems(stamp):
    """What makes this stamp unusable, in reading order, or an empty list where it is fine."""
    if not isinstance(stamp, dict):
        return [f"its root is {type(stamp).__name__} where an object is required"]
    out = []
    for key, want in STAMP_SHAPE.items():
        if key not in stamp:
            out.append(f"{key} is missing")
        elif not isinstance(stamp[key], want):
            out.append(f"{key} is {type(stamp[key]).__name__} where {want.__name__} is required")
    # The version carries the format rather than the content, so a mismatch either way is unreadable.
    # A newer stamp holds fields this code does not know, and an older one lacks fields it reads.
    # Carrying the field and never checking it is the version telling nobody anything.
    if stamp.get("stampVersion") not in (None, STAMP_VERSION) and isinstance(
        stamp.get("stampVersion"), int
    ):
        out.append(
            f"stampVersion is {stamp['stampVersion']} where this installer writes {STAMP_VERSION}"
        )
    return out


def registration_problems(claude_home):
    """Whether settings.json still wires the kit in, which decides if any of it actually runs.

    The hook's bytes being correct says nothing about whether Claude Code invokes it. An entry
    removed from settings.json leaves a machine carrying a complete, current, and entirely inert
    kit, which every other check here reports as fine.
    """
    try:
        raw = read_regular_file(claude_home / "settings.json")
    except FileNotFoundError:
        return ["settings.json is missing, so the hooks are not registered"]
    except OSError as e:
        return [f"settings.json cannot be read ({e})"]
    if raw is None:
        return ["settings.json is not a regular file, so the hooks are not registered"]
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    # ValueError rather than JSONDecodeError, since it also covers UnicodeDecodeError.
    # A partially written or non-UTF-8 file raises that before the JSON parser is ever reached.
    except ValueError as e:
        return [f"settings.json cannot be read ({e})"]
    if not isinstance(data, dict):
        return ["settings.json does not hold an object at its root"]
    out = []
    probed = {}

    def report(note):
        """Append `note` once, since one defect a group carries is not two defects when it holds two entries."""
        if note not in out:
            out.append(note)

    def event_groups(event):
        """The matcher groups under `event`, or None when the settings shape cannot be read.

        A wrong shape is reported as itself rather than counted as zero. Iterating a dict yields
        its keys and a string yields its characters.
        So the loops below would find no registration
        and report the hook as absent, which sends a reader to the wrong fix.
        """
        hooks = data.get("hooks")
        if hooks is None:
            return []
        if not isinstance(hooks, dict):
            # Reported once rather than per event, since both events read the same wrong key.
            report(
                f"settings.json has `hooks` as {type(hooks).__name__} where an object is required, "
                "so no registration can be read"
            )
            return None
        groups = hooks.get(event)
        if groups is None:
            return []
        if not isinstance(groups, list):
            out.append(
                f"settings.json has `hooks.{event}` as {type(groups).__name__} where a list is "
                f"required, so the {event} registration cannot be read"
            )
            return None
        return groups

    def check_hook(event, groups, name, label, subject, absent_effect, judge):
        """Report each way the `event` `groups` fail to run hook `name` exactly once.

        `label` names the hook in an entry defect, and `subject` names it in the count lines, with
        `absent_effect` saying what a missing entry costs. `judge(group, hook)` reports what `event`
        itself requires of an entry that runs the deployed hook, and returns whether it counts as registered.
        """
        if groups is None:
            return  # the shape error is already reported, and a count from it would be meaningless
        # Counted apart from the registrations, since an entry with a problem is still an entry.
        # Counting only the sound ones added a "not registered" line under every problem reported here.
        # That line is false, and it sends a reader to the wrong fix.
        named = 0
        registered = 0
        path = claude_home / "hooks" / name
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks") or []:
                if not isinstance(hook, dict) or not names_hook(hook, name):
                    continue
                named += 1
                if not runs_hook(hook, path) or hook.get("type") != "command":
                    spelled = repr(hook.get("command"))
                    if exec_form(hook):
                        spelled += f" with args {hook['args']!r}"
                    out.append(
                        f"a {event} entry names the {label} but does not run the deployed one "
                        f"({hook.get('type')!r} running {spelled})"
                    )
                    continue
                launcher = registered_launcher(hook, path)
                key = (launcher, exec_form(hook))
                if launcher is not None and key not in probed:
                    spawn = exec_spawn_problem(launcher) if key[1] else None
                    probed[key] = spawn or launcher_problem(launcher)
                if launcher is not None and probed[key]:
                    out.append(
                        f"a {event} entry names a launcher that cannot run the {label} "
                        f"({probed[key]}), so the {label} never runs"
                    )
                    continue
                if judge(group, hook):
                    registered += 1
        if named == 0:
            out.append(f"{subject} is not registered in settings.json, so {absent_effect}")
        elif registered > 1:
            out.append(f"{subject} is registered {registered} times, so it runs more than once")

    def check_pre_tool_hook(groups, name, tools, label, subject, unseen_effect, absent_effect):
        """Report each way the PreToolUse `groups` fail to run hook `name` once on every one of `tools`.

        `label` names the hook in an entry or group defect, and `subject` names it in the count
        lines. `unseen_effect` and `absent_effect` say what a blind matcher and a missing entry cost.
        """

        def judge(group, hook):
            # Every tool is checked, since a matcher seeing only some leaves the others' calls unguarded.
            unseen = [t for t in tools if not matcher_sees_tool(group.get("matcher"), t)]
            if unseen:
                report(
                    f"a PreToolUse group registers the {label} under matcher "
                    f"{group.get('matcher')!r}, which no {'/'.join(unseen)} call matches, so "
                    f"{unseen_effect}"
                )
                return False
            return True

        check_hook("PreToolUse", groups, name, label, subject, absent_effect, judge)

    def judge_sweep(group, hook):
        """Report a SessionEnd sweep entry's timeout and matcher, counting it unless its timeout is too short."""
        # A longer budget than this installer writes is not a defect, so only a shorter one is reported.
        # The event's own default is 1.5s, which is what the sweep needs more than.
        timeout = hook.get("timeout")
        too_short = isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        if not too_short:
            too_short = timeout < SWEEP_TIMEOUT_SECONDS
        if too_short:
            carries = (
                "carries no timeout" if "timeout" not in hook else f"carries timeout {timeout!r}"
            )
            out.append(
                f"the SessionEnd sweep {carries} where this installer writes "
                f"{SWEEP_TIMEOUT_SECONDS}, too little for a `ps` on a loaded machine"
            )
            return False
        # Any matcher other than absent, empty, or `*` may filter some exit reasons out.
        # Counting it as registered reports a machine current while the sweep may never fire on an ordinary exit.
        if not matcher_covers_every_exit_reason(group.get("matcher")):
            report(
                f"the SessionEnd sweep is registered under a matcher "
                f"({group.get('matcher')!r}) other than absent, empty, or `*`, so it may not run on every exit reason"
            )
        return True

    groups = event_groups("PreToolUse")
    check_pre_tool_hook(
        groups,
        GUARD_NAME,
        ("Bash",),
        "write guard",
        "the write guard",
        "that group never fires",
        "the guard never runs",
    )
    check_pre_tool_hook(
        groups,
        TIER_NAME,
        ("Agent", "Workflow"),
        "model tier guard",
        "the model tier guard",
        "that dispatch is never checked",
        "a maintainer-only model dispatch is never refused",
    )
    check_hook(
        "SessionEnd",
        event_groups("SessionEnd"),
        SWEEP_NAME,
        "sweep",
        "the SessionEnd sweep",
        "a surviving shell is never reported",
        judge_sweep,
    )
    capable, reason = containment_capable(claude_home / "hooks" / CONTAIN_NAME)
    env = data.get("env")
    held = env.get(PREFIX_VAR) if isinstance(env, dict) else None
    ours = names_prefix(held, claude_home / "hooks" / CONTAIN_NAME)
    # A foreign value is the maintainer's own choice, which a re-run leaves alone, so it is a note rather than drift.
    if capable and not ours and foreign_prefix(data) is None:
        why = f"is {held!r}, which does not run the deployed prefix" if held else "is not set"
        out.append(
            f"{PREFIX_VAR} {why}, so agent commands run with no task or memory ceiling "
            f"although this host can contain them ({reason})"
        )
    # Any copy of the prefix counts here, since a value synced from another host names a path this one lacks.
    elif not capable and held is not None and foreign_prefix(data) is None:
        out.append(
            f"{PREFIX_VAR} names a containment prefix where it cannot contain ({reason}), "
            "so re-run the installer to correct it"
        )
    out.extend(subagent_model_findings(data.get("env"), claude_home)[0])
    allow = (
        data.get("permissions", {}).get("allow")
        if isinstance(data.get("permissions"), dict)
        else None
    )
    for _, rule in MANAGED_PERMISSIONS:
        if not isinstance(allow, list) or rule not in allow:
            out.append(f"the permission rule {rule} is absent from settings.json")
    misshapen = [
        (path, held, want)
        for path, want in INSTRUCTION_FILES_SHAPE
        if (held := at(data, path)) is not MISSING and not isinstance(held, want)
    ]
    for path, held, want in misshapen[:1]:
        out.append(
            f"settings.json has `{path.replace('/', '.')}` as {type(held).__name__} where "
            f"{want.__name__} is required, so the instructionFiles setting cannot be read and a "
            "re-run refuses the file"
        )
    for plugin in INSTRUCTION_FILES_PLUGINS if not misshapen else ():
        held = at(data, f"pluginConfigs/{plugin}/options/instructionFiles")
        if held != INSTRUCTION_FILES:
            why = "is absent" if held is MISSING else f"is {held!r}"
            out.append(
                f"pluginConfigs.{plugin}.options.instructionFiles {why} rather than "
                f"{INSTRUCTION_FILES}, so a CLAUDE.local.md stops AGENTS.md loading"
            )
    # Only an absence is drift, since a re-run clears it.
    # A host-set value is one a re-run leaves alone, so --report warns of it.
    if session_url_state(data)[0] == "absent":
        out.append(
            "attribution.sessionUrl is absent rather than false, so a web or Remote Control "
            "session adds a session link to the commits and pull requests it writes"
        )
    misshape = auto_mode_misshape(data)
    if misshape is not None:
        where, held, want = misshape
        out.append(
            f"settings.json has `{where}` as {type(held).__name__} where {want.__name__} is "
            "required, so the owned autoMode rules cannot be read and a re-run refuses the file"
        )
    else:
        found = tagged_auto_mode(data)
        for name, want_rules in owned_auto_mode().items():
            if found[name] != want_rules:
                out.append(
                    f"the autoMode.{name} entries tagged {AUTO_MODE_TAG.strip()} differ from what "
                    "this checkout writes"
                )
        # Read live from settings.json rather than folded into the payload digest, which covers only what this kit writes.
        auto = data.get("autoMode")
        for name in AUTO_MODE_LISTS:
            held = auto.get(name) if isinstance(auto, dict) else None
            if isinstance(held, list) and not defaults_first(held):
                out.append(f"autoMode.{name} {defaults_reason(held)}")
    return out


def stamp_line(stamp):
    """One line naming the machine and what it carries, short enough to paste into a checklist.

    Every read is total. The caller validates the shape first, and this stays printable anyway.
    That is because a formatter that raises turns a verdict about a broken stamp into a traceback.
    """
    host = stamp.get("host") or {}
    src = stamp.get("source") or {}
    where = (
        host.get("distro") or f"{host.get('system', 'unknown')} {host.get('release', '')}".strip()
    )
    if host.get("wsl"):
        where += " (WSL)"
    commit = str(src.get("commit") or "unknown")[:7] + ("-dirty" if src.get("dirty") else "")
    held = stamp.get("blocks")
    blocks = (
        ", ".join(f"{k} {v}" for k, v in sorted(held.items()))
        if isinstance(held, dict) and held
        else "none"
    )
    return (
        f"{host.get('hostname', 'unknown')} | {where} | hub {commit} | "
        f"payload {stamp.get('payloadDigest', 'unknown')} | {blocks} | "
        f"{stamp.get('installedUtc', 'unknown')}"
    )


def report(claude_home):
    """Answer whether this machine matches this checkout, reading only.

    Compared on the payload digest rather than on the commit.
    That is because a machine installed from an
    older commit whose kit bytes never changed is current.
    It is also because reporting it as stale sends someone
    to re-run an installer that would write the same file.
    """
    path = claude_home / "agent-safety-stamp.json"
    current = payload_digest()
    print(f"This checkout: payload {current}, hub {(source_ref().get('commit') or 'unknown')[:7]}")
    try:
        raw = read_regular_file(path)
        if raw is None:
            raise NotRegularFile(f"{path} is not a regular file")
        stamp = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        print(f"NOT INSTALLED: no stamp at {path}")
        print("  Run the installer with no arguments to install and stamp this machine.")
        return 2
    except NotRegularFile:
        sys.stderr.write(
            f"Stamp at {path} is not a regular file. Move it aside, then re-run the installer.\n"
        )
        return 2
    # ValueError rather than JSONDecodeError, since it also covers UnicodeDecodeError.
    # A partially written or non-UTF-8 file raises that before the JSON parser is ever reached.
    except (ValueError, OSError) as e:
        sys.stderr.write(
            f"Stamp at {path} is unreadable ({e}). Re-run the installer to rewrite it.\n"
        )
        return 2
    # Valid JSON is not a usable stamp: a hand edit or an older format parses and then breaks the read.
    problems = stamp_problems(stamp)
    if problems:
        sys.stderr.write(
            f"Stamp at {path} is unusable: {'; '.join(problems)}. "
            "Re-run the installer to rewrite it.\n"
        )
        return 2
    print(f"This machine:  {stamp_line(stamp)}")
    # The payload digest decides currency, so neither source shape below is a problem in itself.
    held_source = stamp.get("source", {})
    if held_source.get("vcs") == "archive":
        recorded = held_source.get("commit")
        # With a commit the stamp is comparable, so the note names it.
        # Only a commit-less archive has nothing to compare.
        if isinstance(recorded, str) and recorded:
            print(f"Note: installed from a fetched archive at {recorded[:7]}.")
        else:
            print(
                "Note: installed from a fetched archive that recorded no commit, "
                "so the stamp cannot say which hub revision it holds."
            )
    elif held_source.get("vcs") == "none":
        print(
            "Note: installed from a tree that named no commit, so the stamp cannot say which hub revision it holds."
        )
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
        problems.append(
            "a deployed hook, its data file, or CLAUDE.md is missing or unreadable, so the kit is not fully installed"
        )
    elif live_installed != current:
        problems.append("the installed content differs from what this checkout would write")
    # Correct bytes on disk are not a running guard, so the wiring is checked as well.
    problems.extend(registration_problems(claude_home))
    settings_data = read_settings(claude_home)
    auto = settings_data.get("autoMode") if isinstance(settings_data, dict) else None
    for name in AUTO_MODE_LISTS:
        held = auto.get(name) if isinstance(auto, dict) else None
        if isinstance(held, list):
            untagged = sum(1 for e in held if not tagged(e) and e != AUTO_MODE_DEFAULTS)
            splice = f"with {AUTO_MODE_DEFAULTS}" if defaults_first(held) else defaults_reason(held)
            print(f"autoMode.{name}: {untagged} untagged entries, {splice}")
    foreign = foreign_prefix(settings_data)
    env_block = settings_data.get("env") if isinstance(settings_data, dict) else None
    unset = not isinstance(env_block, dict) or env_block.get(PREFIX_VAR) is None
    capable, reason = containment_capable(claude_home / "hooks" / CONTAIN_NAME)
    # A registered prefix on a host that cannot run it is a STALE problem above, not an uncontained host.
    if unset and not capable:
        print(f"Note: agent commands on this host run uncontained ({reason}).")
    if foreign is not None:
        remedy = (
            "Remove that value and re-run the installer for this kit to contain them."
            if capable
            else f"This kit cannot contain them here now ({reason})."
        )
        print(
            f"Note: {PREFIX_VAR} is {foreign!r}, which this kit does not own, so this kit does not "
            f"contain agent commands. {remedy}"
        )
    for note in subagent_model_findings(env_block, claude_home)[1]:
        print(f"Note: {note}")
    if isinstance(settings_data, dict):
        state, words = session_url_state(settings_data)
        if state == "foreign":
            print(
                f"WARNING: {words}. The host set it, so a re-run leaves it alone. "
                "It may leave a session link on the commits and pull requests a web or Remote Control "
                "session writes, which fleet rules never allow. Claude Code's reading of a value "
                "that is neither a boolean nor an object is not established here. To fix it, set attribution.sessionUrl "
                "to false, first replacing attribution with an object where it is not one. Setting "
                "attribution itself to false also works, but it hides every attribution, the "
                "commit and pull request ones included."
            )
    # Read from the file rather than compared against the stamp.
    # An install onto a corrupted file writes the corruption into the stamp, and the two then agree.
    problems.extend(marker_corruption(claude_home / "CLAUDE.md"))
    # The whole file is compared too, since content outside the blocks is drift no block check sees.
    claude_md = claude_home / "CLAUDE.md"
    local_path = local_instructions_path()
    local_text, local_problem = read_local_instructions(local_path)
    claude_text = read_claude_md(claude_md)
    claude_unreadable = os.path.lexists(claude_md) and claude_text is None
    if local_problem:
        problems.append(local_problem)
    if claude_unreadable:
        problems.append(f"{claude_md} exists but is not a readable regular file")
    elif not local_problem and claude_text is not None:
        live_text = normalized(claude_text)
        if live_text != render_instructions(local_path, local_text):
            if text_digest(live_text) != stamp.get("instructionsDigest"):
                problems.append(
                    "CLAUDE.md was edited since the last install, or predates whole-file "
                    "ownership, so a re-run backs it up before rewriting it. Move host-specific "
                    f"content into {local_path}"
                )
            else:
                problems.append(
                    "CLAUDE.md is this installer's own output rather than a hand edit, and "
                    f"this checkout and {local_path} now render a different one"
                )
    if live != stamp.get("blocks"):
        problems.append(
            f"CLAUDE.md now holds {live or 'no blocks'}, where the stamp recorded {stamp.get('blocks') or 'none'}"
        )
    if stamp.get("source", {}).get("dirty"):
        problems.append(
            "installed from a dirty checkout, so the recorded commit does not identify the bytes"
        )
    if problems:
        print("STALE:")
        for p in problems:
            print(f"  - {p}")
        # The installer refuses an unusable local file, so re-running first would only repeat the refusal.
        if local_problem:
            print(f"  Fix {local_path} first, since the installer refuses it as it stands.")
        if claude_unreadable:
            print(f"  If the re-run refuses {claude_md}, move it aside first.")
        print(
            "  Re-run the installer with no arguments. It is idempotent, and it backs up a "
            "CLAUDE.md edited since the last install before rewriting it."
        )
        return 1
    print("CURRENT: this machine matches this checkout.")
    return 0


def run() -> int:
    parser = argparse.ArgumentParser(
        description="Install the agent host-safety kit, or report whether this machine is current."
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="read-only: compare this machine's stamp against this checkout and exit",
    )
    args = parser.parse_args()

    # The stamp step calls datetime.UTC, a 3.11 API, so an older interpreter is refused up front rather than crashing mid-install.
    # A capability probe rather than a version tuple, which the linter reads as dead code under the py313 target.
    if not hasattr(datetime, "UTC"):
        sys.stderr.write(
            "This installer and the hook require Python 3.11+. Run it with a newer python3.\n"
        )
        return 1

    # Expanduser resolves a CLAUDE_HOME set in `~/...` form to the home dir rather than a literal `~` dir.
    claude_home_env = os.environ.get("CLAUDE_HOME")
    claude_home = (
        pathlib.Path(claude_home_env).expanduser()
        if claude_home_env
        else pathlib.Path.home() / ".claude"
    )
    hooks_dir = claude_home / "hooks"
    hook_dst = hooks_dir / GUARD_NAME
    sweep_dst = hooks_dir / SWEEP_NAME
    contain_dst = hooks_dir / CONTAIN_NAME
    tier_dst = hooks_dir / TIER_NAME
    settings = claude_home / "settings.json"
    claude_md = claude_home / "CLAUDE.md"

    # Reported before anything is created, so a report on an uninstalled machine does not install it.
    if args.report:
        return report(claude_home)

    local_path = local_instructions_path()
    local_text, local_problem = read_local_instructions(local_path)
    if local_problem:
        sys.stderr.write(f"Nothing was installed: {local_problem}.\n")
        return 1
    launcher, rejected = hook_launcher()
    if launcher is None:
        sys.stderr.write(
            "Nothing was installed: no launcher runs the hooks at Python 3.11+ "
            f"({'; '.join(rejected) or 'no candidate found'}).\n"
        )
        return 1
    print(f"Installing agent host-safety kit into: {claude_home}")
    for problem in rejected:
        print(f"  launcher: passed over {problem}")
    print(f"  launcher: {launcher}")
    claude_home.mkdir(parents=True, exist_ok=True)

    # 0. CLAUDE.md is rendered whole: a header, one marker block per snippet, then the host-local file.
    # It goes first, so a file that cannot be read, backed up, or written stops the run before any hook or setting changes.
    # Attempting the real read and write is the check, since a predicted one missed cases the write then raised on.
    # The safety block states restrictions only.
    # The fleet block enables, so it stays separate from a block whose own text says nothing in it widens a permission.
    # A file whose digest matches the stamp is the one the last install wrote, so it is replaced silently.
    # Anything else is a hand edit, or a file from before whole-file ownership, so it is backed up first.
    # Preserve CLAUDE.md's existing line endings: work in \n internally, write back with its own ending.
    # A file that is not valid UTF-8 still decodes for the comparison, and the backup keeps its raw bytes.
    # A file already holding the render is left unwritten, so a read-only file that is current still installs.
    rendered = render_instructions(local_path, local_text)
    # Anything else at the path, a FIFO or a link to a device, would hang the write or swallow it.
    # A dangling link reads as absent, so a dotfiles link whose target is not created yet still writes it.
    try:
        raw = read_regular_file(claude_md)
        if raw is None:
            sys.stderr.write(
                f"Nothing was installed: {claude_md} is not a regular file. Move it aside and re-run.\n"
            )
            return 1
    except FileNotFoundError:
        raw = None
    except OSError as e:
        sys.stderr.write(
            f"Nothing was installed: {claude_md} cannot be read ({e}). Move it aside and re-run.\n"
        )
        return 1
    newline = "\r\n" if raw is not None and b"\r\n" in raw else "\n"
    existing = None if raw is None else normalized(raw.decode("utf-8", errors="replace"))
    backups = []
    if existing == rendered:
        action = "already current"
    else:
        # Backed up only once the open shows the write can happen, so a refused re-run leaves no backup.
        needs_backup = existing is not None and text_digest(
            existing
        ) != stamped_instructions_digest(claude_home)
        encoded = rendered.replace("\n", newline).encode("utf-8")
        try:
            write_regular_file(
                claude_md,
                encoded,
                before=(lambda: backups.append(write_backup(claude_md, raw)))
                if needs_backup
                else None,
            )
        except OSError as e:
            kept = f" Its prior content is backed up at {backups[0]}." if backups else ""
            aside = ", or move the file aside" if os.path.lexists(claude_md) else ""
            verb = "rewritten" if raw is not None else "written"
            sys.stderr.write(
                f"Nothing was installed: {claude_md} could not be {verb} ({e}).{kept} "
                f"Fix what the error names{aside}, then re-run.\n"
            )
            return 1
        if backups:
            action = f"rewritten, the edited prior file backed up to {backups[0]}"
        else:
            action = "updated" if existing is not None else "written"
    backup = backups[0] if backups else None
    print(f"  CLAUDE.md -> {claude_md} ({action})")
    if backup is not None:
        print(f"    Move anything host-specific into {local_path}, then re-run to append it.")

    hooks_dir.mkdir(parents=True, exist_ok=True)

    # 1. Stage every hook beside its live path, self-test each staged copy, and only then replace.
    # Copying onto the live path first and testing afterwards left a failed self-test's broken copy installed.
    # The existing registration still pointed at it, so an install that refused to register disabled the guard.
    # Named per process, since two installs sharing one staged path clobber each other's copy.
    # A SIGKILL between staging and the replace still leaves one behind.
    staged = []
    try:
        for src_name in DEPLOYED_HOOKS:
            tmp = hooks_dir / f"{src_name}.{os.getpid()}.staged"
            staged.append((src_name, tmp))
            try:
                shutil.copyfile(HERE / src_name, tmp)
            except OSError as e:
                for _n, f in staged:
                    f.unlink(missing_ok=True)
                sys.stderr.write(f"staging {src_name} failed ({e}); no hook was replaced.\n")
                return 1
            try:
                os.chmod(tmp, 0o755)
            except OSError:
                pass
            r = subprocess.run(
                [launcher, str(tmp), "--selftest"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                check=False,
            )
            if r.returncode != 0:
                for _n, f in staged:
                    f.unlink(missing_ok=True)
                sys.stderr.write(
                    f"{src_name} self-test FAILED; no hook was replaced.\n" + r.stdout + r.stderr
                )
                return 1
            print(f"  {src_name} self-test: PASS")
        # The data the hooks read goes in after every self-test passed and before any hook is replaced.
        # A new hook then never runs beside a config it cannot find.
        for data_name, data_src in DEPLOYED_DATA.items():
            try:
                write_regular_file(hooks_dir / data_name, data_src.read_bytes())
            except OSError as e:
                for _n, f in staged:
                    f.unlink(missing_ok=True)
                sys.stderr.write(f"copying {data_name} failed ({e}); no hook was replaced.\n")
                return 1
            print(f"  data -> {hooks_dir / data_name}")
        for done_count, (src_name, tmp) in enumerate(staged):
            dst = hooks_dir / src_name
            # `os.replace` is atomic on the same filesystem, so a live hook is never a partial file.
            # It can still fail as a whole, most plausibly on Windows where another process holds the destination open.
            # Each live hook is intact either way, and what needs saying is that some were replaced and some were not.
            try:
                os.replace(tmp, dst)
            except OSError as e:
                for _n, f in staged:
                    f.unlink(missing_ok=True)
                sys.stderr.write(
                    f"replacing {dst} failed ({e}). {done_count} of {len(staged)} hooks were replaced, "
                    "the rest are unchanged, nothing was registered, and the staged copies are removed. "
                    "Re-run the installer.\n"
                )
                return 1
            print(f"  hook -> {dst}")
    except BaseException:
        for _n, f in staged:
            # The interrupt already unwinding is the one to report.
            with contextlib.suppress(OSError):
                f.unlink(missing_ok=True)
        raise

    # 2. Register the hook command in settings.json under exactly one PreToolUse/Bash group.
    hook_cmd = hook_command(launcher, hook_dst)
    # Read into a variable rather than twice off disk, once to test for content and once to parse.
    # Two reads can also disagree, since another process may write between them.
    data = {}
    try:
        settings_raw = read_regular_file(settings)
        if settings_raw is None:
            raise NotRegularFile(f"{settings} is not a regular file")
        settings_text = settings_raw.decode("utf-8")
    except FileNotFoundError:
        settings_raw, settings_text = None, ""
    except (ValueError, OSError) as e:
        sys.stderr.write(f"{settings} cannot be read ({e}). Fix or move it aside, then re-run.\n")
        return 1
    if settings_text.strip():
        try:
            data = json.loads(settings_text)
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
            "is required. Fix or remove it, then re-run. This file is unchanged, so the hooks are "
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
            "Fix or remove that key, then re-run. This file is unchanged, so the hooks are deployed "
            "but not registered.\n"
        )

    for path, want in (
        ("hooks", dict),
        ("hooks/PreToolUse", list),
        ("hooks/SessionEnd", list),
        ("permissions", dict),
        ("permissions/allow", list),
        ("env", dict),
        *INSTRUCTION_FILES_SHAPE,
    ):
        held = at(data, path)
        # An explicit null is present rather than absent, and `setdefault` hands back the null it found.
        # It is therefore rejected here rather than read as a gap the default fills.
        if held is not MISSING and not isinstance(held, want):
            reject(path.replace("/", "."), held, want)
            return 1

    # A list of the right type can still hold the wrong elements.
    # The registration below reads each group as an object, and each group's `hooks` as a list it appends to.
    for event in ("PreToolUse", "SessionEnd"):
        groups = at(data, f"hooks/{event}")
        if groups is MISSING:
            continue
        for i, g in enumerate(groups):
            if not isinstance(g, dict):
                reject(f"hooks.{event}[{i}]", g, dict)
                return 1
            if "hooks" in g and not isinstance(g["hooks"], list):
                reject(f"hooks.{event}[{i}].hooks", g["hooks"], list)
                return 1
            for j, h in enumerate(g.get("hooks") or []):
                if not isinstance(h, dict):
                    reject(f"hooks.{event}[{i}].hooks[{j}]", h, dict)
                    return 1
    misshape = auto_mode_misshape(data)
    if misshape is not None:
        reject(*misshape)
        return 1

    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    register_hook(
        pre,
        GUARD_NAME,
        {"type": "command", **hook_cmd},
        lambda g: g.get("matcher") == "Bash",
        "Bash",
    )
    done = ["PreToolUse/Bash hook registered"]

    # The tier guard gets its own Agent|Workflow group, stripped and re-added the same way so a re-run leaves one.
    register_hook(
        pre,
        TIER_NAME,
        {"type": "command", **hook_command(launcher, tier_dst)},
        lambda g: g.get("matcher") == TIER_MATCHER,
        TIER_MATCHER,
    )
    done.append(f"PreToolUse/{TIER_MATCHER} model tier guard registered")

    # Step 2b registers the SessionEnd sweep the same strip-then-register way as the guard above.
    # The chosen or newly created group covers every exit reason, so the sweep fires on all of them.
    ends = data.setdefault("hooks", {}).setdefault("SessionEnd", [])
    register_hook(
        ends,
        SWEEP_NAME,
        {
            "type": "command",
            **hook_command(launcher, sweep_dst),
            "timeout": SWEEP_TIMEOUT_SECONDS,
        },
        lambda g: matcher_covers_every_exit_reason(g.get("matcher")),
    )
    done.append("SessionEnd sweep registered")

    # Step 2c sets the containment prefix only where this host can start a scope, and never over a value naming anything else.
    capable, reason = containment_capable(contain_dst)
    env = data.setdefault("env", {})
    held = env.get(PREFIX_VAR)
    ours = held is not None and kit_prefix(held)
    if held is not None and not ours:
        done.append(
            f"{PREFIX_VAR} left as {held!r}, which this kit does not own, so this kit does not contain agent commands"
        )
    elif capable:
        env[PREFIX_VAR] = prefix_value(contain_dst)
        done.append(f"{PREFIX_VAR} set, so agent commands run contained ({reason})")
    else:
        env.pop(PREFIX_VAR, None)
        done.append(f"{PREFIX_VAR} not set, so agent commands run uncontained ({reason})")
    tiers = tier_defaults()
    recorded = stamped_subagent_model(claude_home)
    held = env.get(SUBAGENT_VAR, MISSING)
    owned_subagent = recorded if recorded is not None and held == recorded else None
    forced = subagent_force(env)
    if tiers is None:
        done.append(f"{SUBAGENT_VAR} left as it is, since {TIER_CONFIG_NAME} cannot be read")
    elif forced:
        done.append(
            f"{SUBAGENT_VAR} left as it is, since {forced}, which flattens every subagent onto one "
            "model whatever tier a dispatch names"
        )
    else:
        state = subagent_model_state(held, recorded, tiers["default"])
        if state in ("absent", "outdated"):
            env[SUBAGENT_VAR] = tiers["default"]
            owned_subagent = tiers["default"]
            was = "set" if state == "absent" else f"changed from {held!r}, which this kit wrote"
            done.append(
                f"{SUBAGENT_VAR} {was} to {tiers['default']!r}, the {tiers['default_tier']} tier's "
                "model, from the next session launch"
            )
        elif state == "current":
            done.append(f"{SUBAGENT_VAR} {tiers['default']!r}: already current")
        else:
            done.append(f"{SUBAGENT_VAR} left as it is. {foreign_subagent_note(held, tiers)}")
    if not env:
        del data["env"]

    # 3. Permission rules, merged under the prefixes this installer owns.
    # The strip-then-register shape is the hook registration's above, applied to a flat list.
    # Written in the same pass as the hook, so the file is read once and written once.
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    for prefix, rule in MANAGED_PERMISSIONS:
        matched = replace_owned(
            allow, lambda a, p=prefix: isinstance(a, str) and owns(a, p), [rule]
        )
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

    # Step 3b writes the instruction-files setting under each plugin ID, owned whole so a re-run restores it.
    for plugin in INSTRUCTION_FILES_PLUGINS:
        options = (
            data.setdefault("pluginConfigs", {}).setdefault(plugin, {}).setdefault("options", {})
        )
        held = options.get("instructionFiles")
        options["instructionFiles"] = INSTRUCTION_FILES
        if held == INSTRUCTION_FILES:
            action = "already current"
        elif held is None:
            action = "set"
        else:
            action = f"changed from {held!r}"
        done.append(
            f"pluginConfigs.{plugin}.options.instructionFiles {INSTRUCTION_FILES}: {action}"
        )

    # Step 3c writes the session-link opt-out only where the host holds no value of its own.
    state, words = session_url_state(data)
    if state == "absent":
        data.setdefault("attribution", {})["sessionUrl"] = False
        done.append("attribution.sessionUrl false: set, so Claude Code adds no session link")
    elif state == "off":
        done.append(f"{words}: already current")
    else:
        done.append(
            f"{words}, which this host set, so this kit leaves it and adds no opt-out, and --report warns of it"
        )

    # 4. The autoMode rules, stripped by their tag and appended again.
    # A list the kit creates splices in the built-ins, and a list that exists is made to lead with the splice.
    for name, rules in owned_auto_mode().items():
        auto = data.get("autoMode", {})
        held = auto.get(name)
        if held is None:
            if rules:
                data.setdefault("autoMode", {})[name] = [AUTO_MODE_DEFAULTS, *rules]
                done.append(
                    f"autoMode.{name}: created with {AUTO_MODE_DEFAULTS} first, so Claude Code's "
                    "built-in rules still apply"
                )
            continue
        before = list(held)
        dropped = replace_owned(held, tagged, rules)
        if before == held:
            action = "already current"
        else:
            action = "updated" if dropped else "added"
        if rules or dropped:
            done.append(f"autoMode.{name} rules tagged {AUTO_MODE_TAG.strip()}: {action}")
        # Every list that exists leads with the splice, since one without it silently replaces the built-in rules.
        change = put_defaults_first(held)
        if change:
            others = len(held) - 1
            if change == "added first":
                done.append(
                    f"autoMode.{name}: {AUTO_MODE_DEFAULTS} {change}, so Claude Code's built-in rules "
                    f"apply alongside this host's {others} entr{'y' if others == 1 else 'ies'}"
                )
            else:
                done.append(f"autoMode.{name}: {AUTO_MODE_DEFAULTS} {change}, as this kit requires")

    # Reported after the write rather than as each edit is made, since both edits share one write.
    # A line printed before it claims a change that a later failure would leave unmade.
    try:
        write_regular_file(settings, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    except OSError as e:
        sys.stderr.write(
            f"{settings} could not be written ({e}). Fix what the error names, then re-run.\n"
        )
        return 1
    for line in done:
        print(f"  settings -> {settings} ({line})")

    # 5. Stamp the machine, written last so it records a completed install rather than an attempted one.
    # The blocks are read back off disk here, so the stamp reports what CLAUDE.md holds rather than what was intended.
    stamp_path = claude_home / "agent-safety-stamp.json"
    stamp = build_stamp(
        claude_home,
        datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        text_digest(rendered),
        owned_subagent,
    )
    try:
        write_regular_file(stamp_path, (json.dumps(stamp, indent=2) + "\n").encode("utf-8"))
    except OSError as e:
        sys.stderr.write(
            f"The kit is installed and registered, but the stamp could not be written ({e}), so "
            "--report cannot vouch for this machine. Fix what the error names, then re-run.\n"
        )
        return 1
    print(f"  stamp -> {stamp_path}")

    print("\nDone. This machine:")
    print(f"  {stamp_line(stamp)}")
    print("\nRe-check at any time, from a fresh hub checkout, without changing anything:")
    install_path = HERE / "install.py"
    call = "& " if os.name == "nt" else ""
    print(f'  {call}"{launcher}" "{install_path}" --report')
    print("Restart Claude Code sessions on this machine so the hook and CLAUDE.md load.")
    return 0


received: list[int] = []


def on_signal(signum: int, _frame: types.FrameType | None) -> None:
    """Record the signal and raise KeyboardInterrupt, so both signals unwind one path.

    A later signal is passed over, since it would cut the unwinding short.
    """
    if received:
        return
    received.append(signum)
    raise KeyboardInterrupt


def main() -> int | signal.Signals:
    """Run the installer, returning its status, or the signal that interrupted it.

    A SIGINT inherited as ignored, as a background child's is, stays ignored.
    """
    try:
        signal.signal(signal.SIGTERM, on_signal)
        if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
            signal.signal(signal.SIGINT, on_signal)
        return run()
    except KeyboardInterrupt:
        signum = signal.Signals(received[0] if received else signal.SIGINT)
        sys.stderr.write(
            f"Interrupted by {signum.name}, so the install stopped. Re-run it to finish.\n"
        )
        return signum


def reraise(signum: int) -> NoReturn:
    """End this process by `signum`, so a caller sees the signal rather than an exit status."""
    with contextlib.suppress(OSError):
        sys.stdout.flush()
        sys.stderr.flush()
    if sys.platform == "win32":
        sys.exit(130)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    sys.exit(128 + signum)


if __name__ == "__main__":
    status = main()
    if isinstance(status, signal.Signals):
        reraise(status)
    sys.exit(status)
