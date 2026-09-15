#!/usr/bin/env python3
"""SessionEnd sweep: report the shell processes that outlived the session and that nothing will reap.

Registered as a Claude Code SessionEnd hook. It reads the hook payload on stdin, walks the process
table for the agent process this hook is a child of, and reports every descendant running in its own
session, since a shell started by a tool call is put in a session of its own and therefore survives
the agent that started it. It never kills anything: the maintainer decides what on the machine is
still wanted, and a long build backgrounded on purpose looks exactly like a leaked wait from here.

Reporting at all is what needs saying. A SessionEnd hook cannot block, and Claude Code discards its
JSON output, so stderr on exit 2 is the one channel that reaches the maintainer. Exit 2 is inert on
this event (the session is already ending), which is why the sweep uses it for a report rather than
for a refusal. A clean sweep exits 0 and prints nothing.

What it does not see: a process whose own tool-call shell has already exited is reparented to init,
so it is no longer a descendant of the agent and no descendant walk reaches it. The leak this exists
for is the backgrounded tool-call shell itself, which stays a child of the agent for the session's
whole life, and that one it does see.

Linux and WSL only. `etimes` and a decimal `sess` are procps format keywords, which a BSD `ps` does
not carry, so this reports a boundary rather than a sweep on macOS, and Windows has no session test
at all. The kit does not ship a branch for either that nobody has run. See
host-setup/agent-safety/README.md requirement 8.

Run `stray-process-sweep.py --selftest` to verify the reporting matrix without Claude Code.
"""

import json
import os
import re
import subprocess
import sys

# The process-table fields this sweep reads, in this order, with no header line.
_PS_FORMAT = "pid=,ppid=,sess=,etimes=,args="

# An ancestor is the agent process when it is named this, which is what Claude Code's executable is called.
# A packaged launcher naming the agent only in a later argument is matched second, by the pattern below.
# An unrecognized shape then reports a boundary rather than sweeping the wrong tree.
_AGENT_EXE = ("claude", "claude.exe")

# The agent named as a path component or a bare word, for the second pass.
# A `.claude` configuration directory is deliberately not this, since every tool shell mentions one.
_AGENT_IN_ARGS = re.compile(r"(?:^|[/\s])claude(?:-code)?(?:[/\s]|\.(?:js|exe)|$)")


def _read_process_table(runner=None):
    """{pid: (ppid, session, age_seconds, command)} for every process, or None when ps cannot run.

    A failed `ps` is None rather than an empty table. Reading `.stdout` alone turned an unsupported
    format keyword into "no processes", which reads as a clean sweep and reaches the caller as the
    wrong diagnosis, so the exit status and an empty table are both failures here.
    """

    def _run():
        done = subprocess.run(
            ["ps", "-eo", _PS_FORMAT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        return done.returncode, done.stdout

    try:
        code, out = (runner or _run)()
    except (OSError, subprocess.SubprocessError):
        return None
    if code != 0:
        return None
    table = _parse_process_table(out)
    return table or None


def _parse_process_table(out):
    """Parse `ps -eo pid=,ppid=,sess=,etimes=,args=` output, skipping any line that does not fit."""
    table = {}
    for line in (out or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, sess, age = (int(parts[i]) for i in range(4))
        except ValueError:
            continue
        table[pid] = (ppid, sess, age, parts[4])
    return table


def _ancestors(pid, table):
    """Every ancestor pid of `pid`, nearest first, stopping on a cycle or an unknown parent."""
    out = []
    seen = {pid}
    cur = pid
    while cur in table:
        cur = table[cur][0]
        if cur in seen or cur not in table:
            break
        seen.add(cur)
        out.append(cur)
    return out


def _agent_pid(pid, table):
    """The nearest ancestor of `pid` that is the agent process itself, or None when none is."""
    chain = _ancestors(pid, table)
    for candidate in chain:
        argv0 = table[candidate][3].split()[0] if table[candidate][3].split() else ""
        base = argv0.rsplit("/", 1)[-1].lower()
        if base in _AGENT_EXE:
            return candidate
    # The outermost match, and never a shell.
    # Nearest-first returned a tool shell that merely named a `claude` path in its own arguments.
    # A sweep anchored there walks none of its siblings, so it reports a clean machine.
    # A silent clean report is the one answer this must never give wrongly.
    outermost = None
    for candidate in chain:
        args = table[candidate][3]
        argv0 = args.split()[0] if args.split() else ""
        if _is_shell(argv0):
            continue
        if _AGENT_IN_ARGS.search(args.lower()):
            outermost = candidate
    return outermost


def _is_shell(argv0):
    """True if argv0 invokes a shell, which is never the agent process however its arguments read."""
    base = argv0.rsplit("/", 1)[-1].lower().removesuffix(".exe").lstrip("-")
    return base in ("sh", "bash", "zsh", "ksh", "dash", "fish")


def _descendants(root, table):
    """Every descendant pid of `root`, breadth first."""
    children = {}
    for pid, (ppid, _s, _a, _c) in table.items():
        children.setdefault(ppid, []).append(pid)
    out = []
    queue = list(children.get(root, []))
    while queue:
        pid = queue.pop(0)
        out.append(pid)
        queue.extend(children.get(pid, []))
    return out


def survivors(table, agent, self_pid):
    """The roots of the detached subtrees under `agent` that this sweep is not itself part of.

    A descendant sharing the agent's own session goes when the agent goes, so it is not a survivor.
    A descendant in a session of its own does not, and only the top of each such subtree is reported,
    since ending that shell is what ends the loop under it. The sweep's own subtree is excluded: this
    hook runs in a detached session exactly like the shells it is looking for.
    """
    if agent not in table:
        return []
    agent_session = table[agent][1]
    mine = {self_pid, *_ancestors(self_pid, table), *_descendants(self_pid, table)}
    out = []
    for pid in _descendants(agent, table):
        if pid in mine:
            continue
        ppid, session, _age, _cmd = table[pid]
        if session == agent_session:
            continue
        if ppid in table and table[ppid][1] == session and ppid not in mine:
            continue  # its parent is already reported as the root of this same detached subtree
        out.append(pid)
    return out


def _age(seconds):
    """A whole-unit age, so a reader sees `4h12m` rather than a second count to divide."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def report(table, roots):
    """The maintainer-facing report for `roots`, or "" when there is nothing to report."""
    if not roots:
        return ""
    one = len(roots) == 1
    noun = "process tree" if one else "process trees"
    them = "it" if one else "them"
    lines = [
        f"agent-safety: {len(roots)} {noun} outlived this session, and nothing reaps {them}.",
        "",
    ]
    for pid in roots:
        _ppid, _sess, age, cmd = table[pid]
        extra = len(_descendants(pid, table))
        under = f" (+{extra} under it)" if extra else ""
        lines.append(f"  pid {pid}  age {_age(age)}{under}")
        lines.append(f"    {cmd[:160]}")
    lines += [
        "",
        "Each runs outside the agent's own session, so leaving Claude Code does not stop it. Read",
        "the commands above before ending anything that was backgrounded on purpose, then:",
        "",
        "  kill " + " ".join(str(p) for p in roots),
    ]
    return "\n".join(lines)


def main():
    try:
        json.loads(sys.stdin.read() or "{}")  # the payload is read and not otherwise used
    except (ValueError, OSError):
        pass
    # Windows has no session test at all, and a BSD `ps` carries neither `etimes` nor a decimal
    # `sess`, so on both the hook is inert rather than printing a failure at every session end.
    if os.name != "posix" or sys.platform == "darwin":
        return 0
    table = _read_process_table()
    if table is None:
        print(
            "agent-safety: the stray-process sweep could not read the process table.",
            file=sys.stderr,
        )
        return 2
    agent = _agent_pid(os.getpid(), table)
    if agent is None:
        print(
            "agent-safety: the stray-process sweep could not find the agent process among its own "
            "ancestors, so it swept nothing. Surviving shells, if any, are unreported.",
            file=sys.stderr,
        )
        return 2
    text = report(table, survivors(table, agent, os.getpid()))
    if not text:
        return 0
    print(text, file=sys.stderr)
    return 2


def _selftest():
    # A synthetic table, so the matrix runs identically on any machine: pid -> (ppid, session, age, command).
    # 100 is the terminal the agent was launched from, 200 the agent, sharing the terminal's session.
    rows = [
        "100 1 100 900 -bash",
        "200 100 100 800 claude",
        "300 200 100 700 mcp-server --stdio",
        "400 200 400 600 /bin/bash -c until [ -s out ]; do sleep 30; done",
        "410 400 400 1 sleep 30",
        "500 200 500 5 python3 hooks/stray-process-sweep.py",
        "510 500 500 0 ps -eo pid=,ppid=,sess=,etimes=,args=",
    ]
    table = _parse_process_table("\n".join(rows))
    checks = [
        (
            _agent_pid(500, table) == 200,
            "the agent process is found by name among the sweep's ancestors",
        ),
        (survivors(table, 200, 500) == [400], "the detached shell is the one survivor reported"),
        (
            300 not in survivors(table, 200, 500),
            "a child sharing the agent's session is reaped with it",
        ),
        (410 not in survivors(table, 200, 500), "only the root of a detached subtree is reported"),
        (500 not in survivors(table, 200, 500), "the sweep never reports itself"),
        (510 not in survivors(table, 200, 500), "nor anything it spawned"),
        (
            "kill 400" in report(table, survivors(table, 200, 500)),
            "the report hands over a kill line",
        ),
        (
            "1 process tree outlived this session, and nothing reaps it."
            in report(table, survivors(table, 200, 500)),
            "one survivor reads as one, pronoun included",
        ),
        (
            "2 process trees outlived this session, and nothing reaps them."
            in report(table, [400, 300]),
            "and more than one reads as more than one",
        ),
        (
            "+1 under it" in report(table, survivors(table, 200, 500)),
            "and says what is under each root",
        ),
        (report(table, []) == "", "a clean sweep reports nothing at all"),
        (
            _age(59) == "59s" and _age(90) == "1m30s" and _age(15120) == "4h12m",
            "ages read in whole units",
        ),
        (_agent_pid(100, table) is None, "no agent among the ancestors is a boundary, not a guess"),
        (
            _read_process_table(runner=lambda: (1, "")) is None,
            "a ps that exits non-zero is a failure to read, never an empty machine",
        ),
        (
            _read_process_table(runner=lambda: (0, "")) is None,
            "and so is a ps that exits 0 having printed nothing",
        ),
        (
            _read_process_table(runner=lambda: (0, "1 0 1 9 init")) == {1: (0, 1, 9, "init")},
            "a table that reads is returned as itself",
        ),
    ]
    # A packaged launcher, where the agent is a path component of a later argument rather than argv0.
    # The shell below is the trap: every tool shell sources a snapshot out of a `.claude` directory.
    packaged_rows = [
        "100 1 100 900 -bash",
        "200 100 100 800 node /usr/lib/node_modules/claude-code/cli.js",
        "400 200 400 600 /bin/bash -c source ~/.claude/shell-snapshots/snap.sh && x",
        "500 400 400 5 python3 hooks/stray-process-sweep.py",
    ]
    packaged = _parse_process_table("\n".join(packaged_rows))
    # The same launcher, with a tool shell between it and the sweep naming a `claude` path of its own.
    # Nearest-first anchored on that shell and reported its siblings as nothing.
    shelled_rows = [
        "100 1 100 900 -bash",
        "200 100 100 800 node /usr/lib/node_modules/claude-code/cli.js",
        "400 200 400 600 /bin/bash -c cd ~/repos/claude/x && sleep 999",
        "500 400 400 5 python3 hooks/stray-process-sweep.py",
    ]
    shelled = _parse_process_table("\n".join(shelled_rows))
    checks += [
        (
            _agent_pid(500, packaged) == 200,
            "a launcher naming the agent in a path component is found",
        ),
        (
            _agent_pid(500, packaged) != 400,
            "a shell mentioning a .claude directory is not the agent",
        ),
        (
            _agent_pid(500, shelled) == 200,
            "nor is one whose own arguments name a claude path",
        ),
    ]
    ok = True
    for passed, label in checks:
        if not passed:
            ok = False
        print(f"  {'ok  ' if passed else 'FAIL'} {label}")
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv[1:] else main())
