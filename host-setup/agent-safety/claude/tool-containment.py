#!/usr/bin/env python3
"""Shell prefix: run every command Claude Code executes inside a scope with a task and memory ceiling.

Registered as `CLAUDE_CODE_SHELL_PREFIX` in the user settings.json `env`. Claude Code then runs each
Bash tool call and each hook command as `<this file> '<command string>'`, one argument holding the
whole string, including the cwd bookkeeping Claude Code appends to it. This file starts a transient
systemd user scope carrying `TasksMax` and `MemoryMax`, and runs the string under a shell inside it.

A runaway fan-out then meets the ceiling and fails with a visible error, instead of growing until the
host has to be reset. The ceiling belongs to the scope's cgroup rather than to the foreground wait, so
it still holds after the tool moves a timed-out command to the background, and after the command's
own shell has exited and its children were reparented to init. See
host-setup/agent-safety/README.md requirement 9.

The ceilings default to `DEFAULT_TASKS_MAX` and `DEFAULT_MEMORY_MAX`. The maintainer raises them with
`AGENT_CONTAINMENT_TASKS_MAX` and `AGENT_CONTAINMENT_MEMORY_MAX` in the same `env` block. An agent
cannot raise them for itself, since this process reads the environment the session was launched with
and runs before anything in the command string does.

Each scope is named for its session, so the SessionEnd hook stops what a session's commands left
running and nothing else. A Bash tool call sources a Claude Code shell snapshot and a hook command does
not, which is what separates the two names. The sweep stops only the tool-call scopes, since another
SessionEnd hook may still be running in its own scope when it does.

Where no systemd user manager is reachable, the command runs without a ceiling and one line on stderr
says so, since a silent fallback would read as a contained command. Linux only: the installer does not
register this prefix on a host without a user manager.

Run `tool-containment.py --selftest` to verify the decision matrix without Claude Code.
"""

import os
import sys

DEFAULT_TASKS_MAX = "8192"
DEFAULT_MEMORY_MAX = "25%"

TOOL_UNIT = "claude-tool"
HOOK_UNIT = "claude-hook"

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


def _count(text):
    """Whether `text` is a positive decimal count with no leading zero."""
    return text.isascii() and text.isdigit() and not text.startswith("0")


def session_token(session_id):
    """The session id as it appears in a unit name, or "" where there is none to name."""
    return "".join(c for c in session_id or "" if c.isascii() and (c.isalnum() or c == "-"))[:64]


def snapshot_shell(command):
    """The shell a Bash tool call's snapshot names, or None for a command that sources none."""
    for name in ("bash", "zsh"):
        if f"{_SNAPSHOT}{name}-" in command:
            return name
    return None


def unit_kind(command):
    """`TOOL_UNIT` for a Bash tool call, `HOOK_UNIT` for anything else Claude Code runs."""
    return TOOL_UNIT if snapshot_shell(command) else HOOK_UNIT


def pick_shell(command, which=which):
    """The shell the command string was written for.

    A tool call sources a snapshot named for the shell Claude Code chose, so that name decides. Any
    other string is a hook command, which is plain enough for bash.
    """
    return which(snapshot_shell(command) or "bash") or which("bash") or "/bin/sh"


def valid_tasks(value):
    """Whether systemd reads `value` as a `TasksMax`."""
    return value == "infinity" or _count(value)


def valid_memory(value):
    """Whether systemd reads `value` as a `MemoryMax`: bytes, a K/M/G/T size, a percentage, or infinity."""
    if value == "infinity":
        return True
    if value.endswith("%"):
        return _count(value[:-1]) and int(value[:-1]) <= 100
    return _count(value[:-1] if value[-1:] in "KMGT" else value)


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


def plan(command, env, pid, reachable, which=which):
    """(argv, extra_env, notices) for the outer stage, which the caller execs.

    With a manager, argv starts the scope and re-enters this file as the inner stage, the command and
    its shell carried in `extra_env`. Without one, argv is the shell itself, run with no ceiling.
    """
    shell = pick_shell(command, which)
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
        f"--unit={unit_kind(command)}-{token}-{pid}",
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
    run, extra, notices = plan(argv[1], os.environ, os.getpid(), manager_reachable(os.environ))
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
    argv, extra, notes = plan(tool, env, 42, True, fake_which)
    hook_argv, _e, _n = plan(hook, env, 43, True, fake_which)
    bare, bare_extra, bare_notes = plan(tool, env, 44, False, fake_which)
    raised = ceilings(
        {"AGENT_CONTAINMENT_TASKS_MAX": "20000", "AGENT_CONTAINMENT_MEMORY_MAX": "64G"}
    )
    refused = ceilings({"AGENT_CONTAINMENT_TASKS_MAX": "0", "AGENT_CONTAINMENT_MEMORY_MAX": "lots"})
    inner_argv, inner_env = inner({"PATH": "/bin", COMMAND_VAR: tool, SHELL_VAR: "/usr/bin/bash"})
    checks = [
        ("--unit=claude-tool-0a1b-2c3d-42" in argv, "a tool call's scope is named for its session"),
        ("--unit=claude-hook-0a1b-2c3d-43" in hook_argv, "a hook command's scope is named apart"),
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
            plan(ztool, env, 45, True, fake_which)[1][SHELL_VAR] == "/usr/bin/zsh",
            "a zsh one under zsh",
        ),
        (
            plan(hook, env, 46, True, fake_which)[1][SHELL_VAR] == "/usr/bin/bash",
            "a hook under bash",
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
            all(map(valid_memory, ("16G", "1024", "100%", "infinity")))
            and not any(map(valid_memory, ("0", "07G", "101%", "0%", "16GB", "-1", "1.5G"))),
            "a memory size is read the way systemd reads one",
        ),
        (
            all(map(valid_tasks, ("1", "8192", "infinity")))
            and not any(map(valid_tasks, ("0", "08", "1e3", "-5", "\u0663"))),
            "and so is a task count",
        ),
        (
            which("probe-tool", path=os.pathsep.join((empty, bindir))) == probe
            and which("no-such-tool-xyz", path=bindir) is None,
            "PATH is searched in order, and a miss is None",
        ),
        (
            "--unit=claude-tool-nosession-47" in plan(tool, {}, 47, True, fake_which)[0],
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
