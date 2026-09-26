#!/usr/bin/env python3
"""Shell prefix: run every Bash tool call inside a scope with a task and memory ceiling.

Registered as `CLAUDE_CODE_SHELL_PREFIX` in the user settings.json `env`. Claude Code then runs each
Bash tool call, each hook command, and each stdio MCP server launch as `<this file> '<command string>'`,
one argument holding the whole string. For a tool call this file starts a transient systemd user scope
carrying `TasksMax` and `MemoryMax`, and runs the string under a shell inside it.

Everything else runs under its shell directly, with no scope. The write guard is a hook, and a
PreToolUse hook that exits 1 is a non-blocking error, so a scope that failed to start would let the very
command the guard denies go ahead. An MCP server is long-lived and started outside any session, so a
scope would bound it for its whole life and outlive the session that named it. A tool call is told
apart by the Claude Code shell snapshot it sources first. Where a later Claude Code stops sourcing one,
tool calls run bare too, and the SessionEnd sweep's report of surviving processes is what remains.

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
RESTORE_VAR = "AGENT_CONTAINMENT_RUNTIME_RESTORE"

UNREACHABLE = "no systemd user manager is reachable"
UNDELEGATED = "the user manager is not delegated the pids and memory controllers"

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


def is_tool_call(command):
    """Whether Claude Code runs `command` as a Bash tool call rather than as a hook or an MCP server.

    Either sign is enough: the shell snapshot sourced first, or the cwd bookkeeping appended last, so a
    session whose snapshot failed is still contained.
    """
    tail = command.rstrip().rsplit("&&", 1)[-1].strip()
    cwd_tail = tail.startswith("pwd -P >| ") and "claude-" in tail and tail.endswith("-cwd")
    return snapshot_shell(command) is not None or cwd_tail


def in_tool_scope(read=None):
    """Whether this process already runs inside a tool-call scope, as a nested Claude Code session does."""
    try:
        if read:
            text = read("/proc/self/cgroup")
        else:
            with open("/proc/self/cgroup", encoding="utf-8") as f:
                text = f.read()
    except OSError:
        return False
    return any(line.rsplit("/", 1)[-1].startswith(f"{TOOL_UNIT}-") for line in text.splitlines())


def pick_shell(command, which=which):
    """The shell the command string was written for.

    A tool call sources a snapshot named for the shell Claude Code chose, so that name decides. Any
    other string is a hook command, which is plain enough for bash.
    """
    return which(snapshot_shell(command) or "bash") or which("bash") or "/bin/sh"


# The override grammar is one canonical form rather than everything systemd parses.
# Systemd reads a leading zero as octal, rejects some percentages per property, and varies by version.
MIN_TASKS = 64
MIN_MEMORY = 256 * 1024**2
MAX_LIMIT = 2**63
_UNITS = {"M": 1024**2, "G": 1024**3, "T": 1024**4}


def _count(text):
    """`text` as an int when it is decimal digits with no leading zero, else None."""
    if not (text.isascii() and text.isdigit()) or text.startswith("0"):
        return None
    return int(text)


def valid_tasks(value):
    """Whether `value` is `infinity` or a count from `MIN_TASKS` up, the only `TasksMax` forms accepted."""
    count = _count(value)
    return value == "infinity" or (count is not None and MIN_TASKS <= count < MAX_LIMIT)


def valid_memory(value):
    """Whether `value` is `infinity`, a whole `1%` to `99%` of RAM, or a whole count of M, G, or T.

    A unitless count is bytes to systemd, and one written meaning megabytes kills every command at its
    start, so a unit is required, and the size floor is `MIN_MEMORY`.
    """
    if value == "infinity":
        return True
    count = _count(value[:-1])
    if count is None:
        return False
    if value.endswith("%"):
        return 1 <= count <= 99
    unit = _UNITS.get(value[-1])
    return unit is not None and MIN_MEMORY <= count * unit < MAX_LIMIT


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


def manager_runtime(env, platform=sys.platform, which=which, exists=os.path.exists, uid=None):
    """The runtime directory holding this account's systemd user manager socket, or None where there is none.

    `XDG_RUNTIME_DIR` is read first and `/run/user/<uid>` second, since WSLg points the variable at a
    directory of its own while the user manager listens in the standard one.
    """
    if not platform.startswith("linux") or not which("systemd-run"):
        return None
    uid = os.getuid() if uid is None else uid
    for runtime in (env.get("XDG_RUNTIME_DIR"), f"/run/user/{uid}"):
        if runtime and exists(os.path.join(runtime, "systemd", "private")):
            return runtime
    return None


def delegated(uid=None, read=None):
    """Whether the user manager holds the pids and memory controllers, without which a scope enforces neither ceiling."""
    uid = os.getuid() if uid is None else uid
    path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
    try:
        if read:
            text = read(path)
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
    except OSError:
        return False
    return {"pids", "memory"} <= set(text.split())


def plan(command, env, suffix, runtime, which=which, why=UNREACHABLE):
    """(argv, extra_env, notices) for the outer stage, which the caller execs.

    For a tool call with a manager, argv starts the scope and re-enters this file as the inner stage,
    the command and its shell carried in `extra_env`. A hook command, or a tool call with no manager,
    gets the shell itself, and only the tool call is told it runs with no ceiling.
    """
    shell = pick_shell(command, which)
    if not is_tool_call(command):
        return [shell, "-c", command], {}, []
    if runtime is None:
        return (
            [shell, "-c", command],
            {},
            [f"{why}, so this command runs with no task or memory ceiling"],
        )
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
        # Without it a runaway at the memory ceiling spills into the host's swap rather than being killed.
        "--property=MemorySwapMax=0",
        f"--property=TimeoutStopSec={STOP_TIMEOUT}",
        "--",
        sys.executable,
        "-I",
        "-S",
        os.path.abspath(__file__),
        "--inner",
    ]
    extra = {COMMAND_VAR: command, SHELL_VAR: shell}
    # The scope finds the manager through this variable, and the command gets its original value back.
    if env.get("XDG_RUNTIME_DIR") != runtime:
        extra["XDG_RUNTIME_DIR"] = runtime
        extra[RESTORE_VAR] = (
            "set:" + env["XDG_RUNTIME_DIR"] if "XDG_RUNTIME_DIR" in env else "unset:"
        )
    return argv, extra, notices


def decide(command, env, suffix, runtime, is_delegated, nested=lambda: False, which=which):
    """`plan` for this host.

    A tool call already inside a tool-call scope runs directly, since the outer scope's ceiling bounds
    it, and a fresh scope would escape that ceiling and outlive the session that stops the outer one. A
    tool call under a manager lacking the controllers runs bare, and says why.
    """
    if is_tool_call(command) and nested():
        return [pick_shell(command, which), "-c", command], {}, []
    why = UNREACHABLE
    if runtime and is_tool_call(command) and not is_delegated():
        runtime, why = None, UNDELEGATED
    return plan(command, env, suffix, runtime, which, why=why)


def inner(env):
    """(argv, env) for the inner stage: the shell, with the hand-off variables removed."""
    env = dict(env)
    command = env.pop(COMMAND_VAR, None)
    shell = env.pop(SHELL_VAR, None) or "/bin/sh"
    state, _sep, original = env.pop(RESTORE_VAR, "").partition(":")
    if state == "set":
        env["XDG_RUNTIME_DIR"] = original
    elif state == "unset":
        env.pop("XDG_RUNTIME_DIR", None)
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
    run, extra, notices = decide(
        argv[1], os.environ, suffix, manager_runtime(os.environ), delegated, in_tool_scope
    )
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
    mcp = "'npx' '-y' 'example-mcp-server'"

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

    RUN = "/run/user/1000"
    env = {"CLAUDE_CODE_SESSION_ID": "0a1b-2c3d", "XDG_RUNTIME_DIR": RUN}
    wslg = dict(env, XDG_RUNTIME_DIR="/mnt/wslg/runtime-dir")
    wslg_extra = plan(tool, wslg, "50", RUN, fake_which)[1]
    restored = inner({**wslg, **wslg_extra})[1]
    unset_extra = plan(tool, {"CLAUDE_CODE_SESSION_ID": "x"}, "51", RUN, fake_which)[1]

    def socket_in(*dirs):
        return lambda path: any(path == os.path.join(d, "systemd", "private") for d in dirs)

    hook_env = dict(env, CLAUDE_PROJECT_DIR="/opt/agent/project")
    # A zsh tool call whose own command names a bash snapshot file after the leading clause.
    mixed = ztool.replace("eval 'echo", "eval 'ls /x/shell-snapshots/snapshot-bash-1.sh; echo")
    argv, extra, notes = plan(tool, env, "42-ab", RUN, fake_which)
    hook_plan = plan(hook, hook_env, "43-cd", RUN, fake_which)
    bare, bare_extra, bare_notes = plan(tool, env, "44-ef", None, fake_which)
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
            plan(hook, hook_env, "43-cd", None, fake_which)[2] == [],
            "and prints nothing where no manager is reachable",
        ),
        (
            plan(mcp, env, "48-ab", RUN, fake_which) == (["/usr/bin/bash", "-c", mcp], {}, []),
            "an MCP server launch, sourcing no snapshot, runs under its shell with no scope",
        ),
        (
            "--unit=claude-tool-0a1b-2c3d-49-ab"
            in plan(tool, hook_env, "49-ab", RUN, fake_which)[0],
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
            plan(ztool, env, "45", RUN, fake_which)[1][SHELL_VAR] == "/usr/bin/zsh",
            "a zsh one under zsh",
        ),
        (
            plan(mixed, env, "46", RUN, fake_which)[1][SHELL_VAR] == "/usr/bin/zsh",
            "only the leading source clause picks the shell",
        ),
        (notes == [], "a contained command prints nothing extra"),
        (
            bare == ["/usr/bin/bash", "-c", tool] and bare_extra == {},
            "no manager runs the shell bare",
        ),
        (len(bare_notes) == 1 and "no task or memory ceiling" in bare_notes[0], "and says so"),
        (
            UNDELEGATED in plan(tool, env, "52", None, fake_which, why=UNDELEGATED)[2][0],
            "a manager lacking the controllers is named as the reason, per command",
        ),
        (
            decide(tool, env, "53", RUN, lambda: False, which=fake_which)
            == (
                ["/usr/bin/bash", "-c", tool],
                {},
                [f"{UNDELEGATED}, so this command runs with no task or memory ceiling"],
            ),
            "a manager that lost the controllers runs a tool call bare and says why",
        ),
        (
            "--unit=claude-tool-0a1b-2c3d-54"
            in decide(tool, env, "54", RUN, lambda: True, which=fake_which)[0],
            "a manager holding them contains it",
        ),
        (
            decide(hook, hook_env, "55", RUN, lambda: 1 / 0, which=fake_which)[2] == [],
            "a hook command never reads the controllers",
        ),
        (
            decide(tool, env, "56", RUN, lambda: True, lambda: True, fake_which)
            == (["/usr/bin/bash", "-c", tool], {}, []),
            "a tool call already inside a tool-call scope runs within it rather than escaping it",
        ),
        (
            in_tool_scope(read=lambda p: "0::/user.slice/app.slice/claude-tool-a-1-ff.scope\n")
            and not in_tool_scope(read=lambda p: "0::/user.slice/app.slice/session-3.scope\n"),
            "a nested session is recognised by the scope it runs in",
        ),
        (
            "--property=MemorySwapMax=0" in argv,
            "a runaway at the memory ceiling is killed rather than spilled into swap",
        ),
        (
            is_tool_call("eval 'ls' < /dev/null && pwd -P >| /tmp/claude-9d32-cwd")
            and not is_tool_call(mcp),
            "a tool call without its snapshot is still recognised by its cwd bookkeeping",
        ),
        (
            delegated(1000, read=lambda p: "cpu memory pids\n")
            and not delegated(1000, read=lambda p: "cpu\n"),
            "delegation needs both the pids and the memory controller",
        ),
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
            all(map(valid_memory, ("16G", "512M", "1T", "25%", "99%", "infinity")))
            and not any(
                map(
                    valid_memory,
                    (
                        "0",
                        "1024",
                        "1.5G",
                        "0.5G",
                        "08G",
                        "1K",
                        "1P",
                        "100%",
                        "050%",
                        "08%",
                        "0.25%",
                        "16GB",
                        "G",
                        "lots",
                        "99999999999T",
                    ),
                )
            ),
            "only the canonical memory forms are accepted",
        ),
        (
            all(map(valid_tasks, ("64", "8192", "infinity")))
            and not any(
                map(
                    valid_tasks,
                    (
                        "0",
                        "1",
                        "1.5",
                        "1e3",
                        "-5",
                        ".5%",
                        "\u0663",
                        "99999999999999999999",
                        "08192",
                        "0100",
                        "50%",
                        "100%",
                    ),
                )
            ),
            "and so is a task count",
        ),
        (
            which("probe-tool", path=os.pathsep.join((empty, bindir))) == probe
            and which("no-such-tool-xyz", path=bindir) is None,
            "PATH is searched in order, and a miss is None",
        ),
        (
            "--unit=claude-tool-nosession-47" in plan(tool, {}, "47", RUN, fake_which)[0],
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
            manager_runtime(env, "darwin", fake_which, lambda p: True, uid=1000) is None,
            "a non-Linux host has no manager",
        ),
        (
            manager_runtime(env, "linux", fake_which, socket_in(RUN), uid=1000) == RUN,
            "a Linux session with a manager socket has one",
        ),
        (
            manager_runtime(wslg, "linux", fake_which, socket_in(RUN), uid=1000) == RUN,
            "a runtime directory lacking the socket falls back to the account's standard one",
        ),
        (
            manager_runtime({}, "linux", fake_which, socket_in(), uid=1000) is None,
            "and no socket anywhere is no manager",
        ),
        (
            wslg_extra.get("XDG_RUNTIME_DIR") == RUN
            and restored.get("XDG_RUNTIME_DIR") == "/mnt/wslg/runtime-dir"
            and RESTORE_VAR not in restored,
            "systemd-run is pointed at the manager, and the command gets the original directory back",
        ),
        (
            "XDG_RUNTIME_DIR" not in inner({"PATH": "/bin", **unset_extra})[1],
            "and an unset directory stays unset for the command",
        ),
        (
            RESTORE_VAR not in extra and "XDG_RUNTIME_DIR" not in extra,
            "a session already pointing at the manager carries no restore",
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
