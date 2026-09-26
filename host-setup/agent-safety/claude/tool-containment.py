#!/usr/bin/env python3
"""Shell prefix: run every Bash tool call inside a scope with a task and memory ceiling.

Registered as `CLAUDE_CODE_SHELL_PREFIX` in the user settings.json `env`. Claude Code then runs each
Bash tool call and each hook command as `<this file> '<command string>'`, one argument holding the
whole string, including the cwd bookkeeping Claude Code appends to it. For a tool call this file starts
a transient systemd user scope carrying `TasksMax` and `MemoryMax`, and runs the string under a shell
inside it.

A hook command runs under its shell directly, with no scope. The write guard is a hook, and a PreToolUse
hook that exits 1 is a non-blocking error, so a scope that failed to start would let the very command
the guard denies go ahead. A hook is told from a tool call by the `CLAUDE_PROJECT_DIR` Claude Code sets
for hooks and by the shell snapshot a tool call sources first. Either sign of a tool call is enough to
contain it, so a change to one of them fails toward containment rather than away from it.

A runaway fan-out then meets the ceiling and fails with a visible error, instead of growing until the
host has to be reset. The ceiling belongs to the scope's cgroup rather than to the foreground wait, so
it still holds after the tool moves a timed-out command to the background, and after the command's
own shell has exited and its children were reparented to init. See
host-setup/agent-safety/README.md requirement 9.

The ceilings default to `DEFAULT_TASKS_MAX` and `DEFAULT_MEMORY_MAX`. The maintainer raises them with
`AGENT_CONTAINMENT_TASKS_MAX` and `AGENT_CONTAINMENT_MEMORY_MAX` in the same `env` block, which an inline
`VAR=x` in the command cannot change, since this process reads them before the command runs. The
ceilings bound an accident rather than an adversary: a command can still raise its own scope's
properties through `systemctl --user set-property`, or start a scope of its own outside this one.

Each scope is named for its session plus a random suffix, so the SessionEnd hook stops what a session's
tool calls left running and nothing else, and a reused pid never collides with a scope still loaded.

Where no systemd user manager is reachable, a tool call runs without a ceiling and one line on stderr
says so, since a silent fallback would read as a contained command. Linux only: the installer does not
register this prefix on a host without a user manager.

Run `tool-containment.py --selftest` to verify the decision matrix without Claude Code.
"""

import os
import sys

DEFAULT_TASKS_MAX = "8192"
DEFAULT_MEMORY_MAX = "25%"

TOOL_UNIT = "claude-tool"

# Since version 254, systemd-run expands `${VAR}` in a scope's arguments by default, rewriting a command before its shell sees it.
# The flag turning that off is an error on an older systemd, so the command travels in the environment instead.
COMMAND_VAR = "AGENT_CONTAINMENT_COMMAND"
SHELL_VAR = "AGENT_CONTAINMENT_SHELL"

# How long a stop waits on SIGTERM before SIGKILL, so the session-end stop fits its hook's budget.
STOP_TIMEOUT = "2s"

_SNAPSHOT = "/shell-snapshots/snapshot-"


def which(name, path=None):
    """The executable `name` resolves to on PATH, or None, without importing `shutil`."""
    for d in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep):
        candidate = os.path.join(d or ".", name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def session_token(session_id):
    """The session id as it appears in a unit name, or "" where there is none to name."""
    return "".join(c for c in session_id or "" if c.isascii() and (c.isalnum() or c == "-"))[:64]


def snapshot_shell(command):
    """The shell named by the snapshot a Bash tool call sources first, or None where it sources none.

    Only the leading `source` clause is read, since the command after it may name any snapshot file.
    """
    if not command.startswith("source "):
        return None
    lead = command.split("&&", 1)[0]
    for name in ("bash", "zsh"):
        if f"{_SNAPSHOT}{name}-" in lead:
            return name
    return None


def is_tool_call(command, env):
    """Whether Claude Code runs `command` as a Bash tool call rather than as a hook command."""
    return snapshot_shell(command) is not None or "CLAUDE_PROJECT_DIR" not in env


def pick_shell(command, which=which):
    """The shell the command string was written for.

    A tool call sources a snapshot named for the shell Claude Code chose, so that name decides. Any
    other string is a hook command, which is plain enough for bash.
    """
    return which(snapshot_shell(command) or "bash") or which("bash") or "/bin/sh"


# The smallest override accepted, so a unitless or mistyped value cannot kill every command at its start.
MIN_TASKS = 64
MIN_MEMORY = 256 * 1024**2
_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5, "E": 1024**6}


def _number(text):
    """`text` as a positive number when it is plain decimal digits with at most one inner point, else None."""
    whole, _dot, frac = text.partition(".")
    if not (whole.isascii() and whole.isdigit()) or (
        _dot and not (frac.isascii() and frac.isdigit())
    ):
        return None
    value = float(text)
    return value if value > 0 else None


def _percent(value):
    """Whether `value` is a percentage above 0 and at most 100, fractional or whole."""
    number = _number(value[:-1]) if value.endswith("%") else None
    return number is not None and number <= 100


def valid_tasks(value):
    """Whether `value` is a `TasksMax` of at least `MIN_TASKS`, a percentage of the system limit, or infinity."""
    if value == "infinity" or _percent(value):
        return True
    return value.isascii() and value.isdigit() and int(value) >= MIN_TASKS


def valid_memory(value):
    """Whether `value` is a `MemoryMax` of at least `MIN_MEMORY` in K to E units, a percentage of RAM, or infinity.

    A unitless count is bytes to systemd, and one written meaning megabytes kills every command at its
    start, so a unit is required.
    """
    if value == "infinity" or _percent(value):
        return True
    number = _number(value[:-1]) if value[-1:] in _UNITS else None
    return number is not None and number * _UNITS[value[-1]] >= MIN_MEMORY


def ceilings(env):
    """(tasks, memory, notices): the ceilings to apply, and a line for each override refused.

    A malformed override falls back to the default rather than to no ceiling, and says so, since
    systemd would otherwise reject it and the command would not run at all.
    """
    notices = []
    tasks = env.get("AGENT_CONTAINMENT_TASKS_MAX") or DEFAULT_TASKS_MAX
    if not valid_tasks(tasks):
        notices.append(f"AGENT_CONTAINMENT_TASKS_MAX={tasks!r} is not a task count")
        tasks = DEFAULT_TASKS_MAX
    memory = env.get("AGENT_CONTAINMENT_MEMORY_MAX") or DEFAULT_MEMORY_MAX
    if not valid_memory(memory):
        notices.append(f"AGENT_CONTAINMENT_MEMORY_MAX={memory!r} is not a memory size")
        memory = DEFAULT_MEMORY_MAX
    return tasks, memory, notices


def manager_reachable(env, platform=sys.platform, which=which, exists=os.path.exists):
    """Whether a systemd user manager can start a scope for this process."""
    runtime = env.get("XDG_RUNTIME_DIR")
    return bool(
        platform.startswith("linux")
        and which("systemd-run")
        and runtime
        and exists(os.path.join(runtime, "systemd", "private"))
    )


def plan(command, env, suffix, reachable, which=which):
    """(argv, extra_env, notices) for the outer stage, which the caller execs.

    For a tool call with a manager, argv starts the scope and re-enters this file as the inner stage,
    the command and its shell carried in `extra_env`. A hook command, or a tool call with no manager,
    gets the shell itself, and only the tool call is told it runs with no ceiling.
    """
    shell = pick_shell(command, which)
    if not is_tool_call(command, env):
        return [shell, "-c", command], {}, []
    if not reachable:
        note = "no systemd user manager is reachable, so this command runs with no task or memory ceiling"
        return [shell, "-c", command], {}, [note]
    tasks, memory, notices = ceilings(env)
    token = session_token(env.get("CLAUDE_CODE_SESSION_ID")) or "nosession"
    argv = [
        which("systemd-run") or "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        f"--unit={TOOL_UNIT}-{token}-{suffix}",
        "--description=Claude Code command",
        f"--property=TasksMax={tasks}",
        f"--property=MemoryMax={memory}",
        f"--property=TimeoutStopSec={STOP_TIMEOUT}",
        "--",
        sys.executable,
        "-I",
        "-S",
        os.path.abspath(__file__),
        "--inner",
    ]
    return argv, {COMMAND_VAR: command, SHELL_VAR: shell}, notices


def inner(env):
    """(argv, env) for the inner stage: the shell, with the hand-off variables removed."""
    env = dict(env)
    command = env.pop(COMMAND_VAR, None)
    shell = env.pop(SHELL_VAR, None) or "/bin/sh"
    if command is None:
        return None, env
    return [shell, "-c", command], env


def main(argv):
    if argv[1:] == ["--inner"]:
        run, env = inner(os.environ)
        if run is None:
            print(f"tool-containment: --inner reached with no {COMMAND_VAR}", file=sys.stderr)
            return 2
        os.execve(run[0], run, env)
    if len(argv) != 2:
        print("usage: tool-containment.py '<command string>' | --selftest", file=sys.stderr)
        return 2
    suffix = f"{os.getpid()}-{os.urandom(4).hex()}"
    run, extra, notices = plan(argv[1], os.environ, suffix, manager_reachable(os.environ))
    for note in notices:
        print(f"tool-containment: {note}", file=sys.stderr)
    os.execve(run[0], run, {**os.environ, **extra})
    return 2  # unreachable, since execve either replaces this process or raises


def _selftest():
    tool = (
        "source /opt/agent/.claude/shell-snapshots/snapshot-bash-1-x.sh 2>/dev/null || true && "
        "eval 'echo ${HOME}' < /dev/null && pwd -P >| /tmp/claude-1-cwd"
    )
    ztool = tool.replace("snapshot-bash-", "snapshot-zsh-")
    hook = '"python3" "/opt/agent/.claude/hooks/gh-write-guard.py"'

    def fake_which(name):
        return {
            "bash": "/usr/bin/bash",
            "zsh": "/usr/bin/zsh",
            "systemd-run": "/usr/bin/systemd-run",
        }.get(name)

    import shutil
    import tempfile

    scratch = tempfile.mkdtemp()
    empty, bindir = os.path.join(scratch, "empty"), os.path.join(scratch, "bin")
    os.mkdir(empty)
    os.mkdir(bindir)
    probe = os.path.join(bindir, "probe-tool")
    with open(probe, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n")
    os.chmod(probe, 0o755)

    env = {"CLAUDE_CODE_SESSION_ID": "0a1b-2c3d", "XDG_RUNTIME_DIR": "/run/user/1000"}
    hook_env = dict(env, CLAUDE_PROJECT_DIR="/opt/agent/project")
    # A zsh tool call whose own command names a bash snapshot file after the leading clause.
    mixed = ztool.replace("eval 'echo", "eval 'ls /x/shell-snapshots/snapshot-bash-1.sh; echo")
    argv, extra, notes = plan(tool, env, "42-ab", True, fake_which)
    hook_plan = plan(hook, hook_env, "43-cd", True, fake_which)
    bare, bare_extra, bare_notes = plan(tool, env, "44-ef", False, fake_which)
    raised = ceilings(
        {"AGENT_CONTAINMENT_TASKS_MAX": "20000", "AGENT_CONTAINMENT_MEMORY_MAX": "64G"}
    )
    refused = ceilings({"AGENT_CONTAINMENT_TASKS_MAX": "0", "AGENT_CONTAINMENT_MEMORY_MAX": "lots"})
    inner_argv, inner_env = inner({"PATH": "/bin", COMMAND_VAR: tool, SHELL_VAR: "/usr/bin/bash"})
    checks = [
        (
            "--unit=claude-tool-0a1b-2c3d-42-ab" in argv,
            "a tool call's scope is named for its session",
        ),
        (
            hook_plan == (["/usr/bin/bash", "-c", hook], {}, []),
            "a hook command runs under its shell with no scope, so no systemd failure reaches it",
        ),
        (
            plan(hook, hook_env, "43-cd", False, fake_which)[2] == [],
            "and prints nothing where no manager is reachable",
        ),
        (
            "--unit=claude-tool-0a1b-2c3d-48-ab" in plan(hook, env, "48-ab", True, fake_which)[0],
            "a command with no snapshot and no hook's project dir is still contained",
        ),
        (
            "--unit=claude-tool-0a1b-2c3d-49-ab"
            in plan(tool, hook_env, "49-ab", True, fake_which)[0],
            "and so is one sourcing a snapshot whatever its environment",
        ),
        (f"--property=TasksMax={DEFAULT_TASKS_MAX}" in argv, "the task ceiling is applied"),
        (f"--property=MemoryMax={DEFAULT_MEMORY_MAX}" in argv, "the memory ceiling is applied"),
        (
            f"--property=TimeoutStopSec={STOP_TIMEOUT}" in argv,
            "a stop escalates to SIGKILL quickly",
        ),
        (tool not in argv and extra[COMMAND_VAR] == tool, "the command travels in the environment"),
        (not any("$" in a for a in argv), "no argument carries a `$` for systemd-run to expand"),
        (extra[SHELL_VAR] == "/usr/bin/bash", "a bash snapshot runs under bash"),
        (
            plan(ztool, env, "45", True, fake_which)[1][SHELL_VAR] == "/usr/bin/zsh",
            "a zsh one under zsh",
        ),
        (
            plan(mixed, env, "46", True, fake_which)[1][SHELL_VAR] == "/usr/bin/zsh",
            "only the leading source clause picks the shell",
        ),
        (notes == [], "a contained command prints nothing extra"),
        (
            bare == ["/usr/bin/bash", "-c", tool] and bare_extra == {},
            "no manager runs the shell bare",
        ),
        (len(bare_notes) == 1 and "no task or memory ceiling" in bare_notes[0], "and says so"),
        (
            raised[:2] == ("20000", "64G") and raised[2] == [],
            "the maintainer can raise both ceilings",
        ),
        (
            refused[:2] == (DEFAULT_TASKS_MAX, DEFAULT_MEMORY_MAX),
            "a malformed override keeps the default",
        ),
        (len(refused[2]) == 2, "and names each refusal"),
        (
            ceilings({"AGENT_CONTAINMENT_MEMORY_MAX": "40%"})[1] == "40%",
            "a percentage is a memory size",
        ),
        (session_token("a/b c;d") == "abcd", "a session id cannot inject into a unit name"),
        (
            all(
                map(
                    valid_memory, ("16G", "1.5G", "0.5G", "512M", "12.5%", "1P", "100%", "infinity")
                )
            )
            and not any(
                map(
                    valid_memory,
                    ("0", "1024", "0.5", ".5G", "1K", "101%", "0%", "16GB", "G", "lots"),
                )
            ),
            "a memory size systemd accepts is accepted, and a malformed one is not",
        ),
        (
            all(map(valid_tasks, ("64", "8192", "50%", "infinity")))
            and not any(map(valid_tasks, ("0", "1", "1.5", "1e3", "-5", ".5%", "\u0663"))),
            "and so is a task count",
        ),
        (
            which("probe-tool", path=os.pathsep.join((empty, bindir))) == probe
            and which("no-such-tool-xyz", path=bindir) is None,
            "PATH is searched in order, and a miss is None",
        ),
        (
            "--unit=claude-tool-nosession-47" in plan(tool, {}, "47", True, fake_which)[0],
            "a missing session id names no session the sweep would stop",
        ),
        (inner_argv == ["/usr/bin/bash", "-c", tool], "the inner stage runs the command verbatim"),
        (
            COMMAND_VAR not in inner_env and SHELL_VAR not in inner_env,
            "and does not leak the hand-off",
        ),
        (
            inner({"PATH": "/bin"})[0] is None,
            "an inner stage with no command refuses rather than runs",
        ),
        (
            not manager_reachable(env, platform="darwin", which=fake_which, exists=lambda p: True),
            "a non-Linux host has no manager",
        ),
        (
            not manager_reachable({}, platform="linux", which=fake_which, exists=lambda p: True),
            "nor does a session with no runtime directory",
        ),
        (
            manager_reachable(env, platform="linux", which=fake_which, exists=lambda p: True),
            "a Linux session with a manager socket has one",
        ),
    ]
    shutil.rmtree(scratch)
    ok = True
    for passed, label in checks:
        ok = ok and passed
        print(f"  {'ok  ' if passed else 'FAIL'} {label}")
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if sys.argv[1:] == ["--selftest"] else main(sys.argv))
