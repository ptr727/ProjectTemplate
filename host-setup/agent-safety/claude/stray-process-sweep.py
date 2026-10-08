#!/usr/bin/env python3
"""SessionEnd sweep: stop the session's contained commands, then report whatever else outlived it.

The first phase stops every command scope `tool-containment.py` started for this session's Bash tool
calls. Each is known to be this session's own by its unit name, so stopping it needs no judgment about
what the process is, and a backgrounded runaway ends with the session that started it. What was stopped
is reported. See host-setup/agent-safety/README.md requirement 9.

Between the two, the sweep stops what this session's tree left behind. A `claude` started from a tool
call runs its own commands in scopes named for itself, all in one slice named for the root session.
Stopping this session's scopes kills such a nested `claude` before its own SessionEnd hook runs, so
its scopes would keep running, named for a session nothing will sweep. Each scope records the pid of
the agent that ran it, and a scope in this session's tree whose agent is no longer running is stopped
and reported with the nested session's id. A scope whose agent still runs is left alone. The root
session then stops its tree's slice and reverts the ceiling put on it, once nothing is left in it.

The second phase is the report-only sweep below, for everything outside those scopes.

`stray-process-sweep.py --orphans` runs the same test across every tree on the machine and reports
each command scope whose agent is gone, without stopping anything, for a session that ended with no
sweep at all, such as one whose terminal was closed.

Registered as a Claude Code SessionEnd hook. It reads the hook payload on stdin, walks the process
table for the agent process this hook is a child of, and reports every descendant running in its own
session, since a shell started by a tool call is put in a session of its own and therefore survives
the agent that started it. This phase never kills anything: the maintainer decides what on the machine
is still wanted, and a long build backgrounded on purpose looks exactly like a leaked wait from here.

Reporting at all is what needs saying. A SessionEnd hook cannot block, and Claude Code discards its
JSON output, so stderr on exit 2 is the one channel that reaches the maintainer. Exit 2 is inert on
this event (the session is already ending), which is why the sweep uses it for a report rather than
for a refusal. A clean sweep exits 0 and prints nothing.

What it does not see: a process whose own tool-call shell has already exited is reparented to init,
so it is no longer a descendant of the agent and no descendant walk reaches it. The leak this exists
for is the backgrounded tool-call shell itself, which stays a child of the agent for the session's
whole life, and that one it does see.

Linux and WSL only. `etimes` and a decimal `sess` are procps format keywords, which a BSD `ps` does
not carry, and Windows has no session test at all, so on macOS and on Windows this exits silently
without sweeping rather than failing at the end of every session. The kit does not ship a branch for
either that nobody has run. See host-setup/agent-safety/README.md requirement 8.

Run `stray-process-sweep.py --selftest` to verify the reporting matrix without Claude Code.
"""

import json
import os
import re
import shutil
import subprocess
import sys

_TOOL_UNIT = "claude-tool"
_TREE_UNIT = "claude-tree"

ROOT_VAR = "AGENT_CONTAINMENT_ROOT"

_AGENT_RECORD = re.compile(r"agent pid (\d+)\s*$")

# The process-table fields this sweep reads, in this order, with no header line.
_PS_FORMAT = "pid=,ppid=,sess=,etimes=,args="

# An ancestor is the agent process when it is named this, which is what Claude Code's executable is called.
# A packaged launcher naming the agent only in a later argument is matched second, by the pattern below.
# An unrecognized shape then reports a boundary rather than sweeping the wrong tree.
_AGENT_EXE = ("claude", "claude.exe")

# The agent named as a path component or a bare word, for the second pass.
# A `.claude` configuration directory is deliberately not this, since every tool shell mentions one.
_AGENT_IN_ARGS = re.compile(r"(?:^|[/\\\s])claude(?:-code)?(?:[/\\\s]|\.(?:js|exe)|$)")


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
        base = argv0.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
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


def is_agent(args):
    """Whether a process with these arguments is an agent: named for it, or a launcher naming it, never a shell."""
    argv0 = args.split()[0] if args.split() else ""
    if argv0.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower() in _AGENT_EXE:
        return True
    return not _is_shell(argv0) and bool(_AGENT_IN_ARGS.search(args.lower()))


def _is_shell(argv0):
    """True if argv0 invokes a shell, which is never the agent process however its arguments read."""
    base = argv0.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe").lstrip("-")
    return base in ("sh", "bash", "zsh", "ksh", "dash", "fish")


def _descendants(root, table):
    """Every descendant pid of `root`, breadth first."""
    children = {}
    for pid, (ppid, _s, _a, _c) in table.items():
        children.setdefault(ppid, []).append(pid)
    out = []
    queue = list(children.get(root, []))
    # Walked by index rather than by `pop(0)`, which shifts the whole list on every step.
    # A busy host's table is large enough for that quadratic walk to be worth the index.
    head = 0
    while head < len(queue):
        pid = queue[head]
        head += 1
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


def _systemctl(*args, runtime=None):
    """(exit code, stdout) of `systemctl --user <args>`, or (None, "") when it cannot run at all.

    `runtime` points it at the manager found by `manager_runtime`, which may not be the session's own
    `XDG_RUNTIME_DIR`.
    """
    env = dict(os.environ, XDG_RUNTIME_DIR=runtime) if runtime else None
    try:
        done = subprocess.run(
            ["systemctl", "--user", *args],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode, done.stdout


def manager_runtime(env, which=shutil.which, exists=os.path.exists, uid=None):
    """The runtime directory holding the user manager socket, found as `tool-containment.py` finds it.

    A host without one never ran a contained command, and asking `systemctl --user` there fails, which
    would report a failure to list at every session end on a host the installer never contained.
    """
    if not which("systemctl"):
        return None
    uid = os.getuid() if uid is None else uid
    for runtime in (env.get("XDG_RUNTIME_DIR"), f"/run/user/{uid}"):
        if runtime and exists(os.path.join(runtime, "systemd", "private")):
            return runtime
    return None


def should_reap(runtime, env):
    """Whether the stop phase runs: where a manager was found, or where the session ran under the prefix.

    The second covers a session under the prefix with no manager found at its end, which is reported as
    an unknown, since the manager may never have run, rather than skipped in silence.
    """
    return bool(runtime) or prefix_registered(env)


def prefix_registered(env):
    """Whether this session runs under the containment prefix, whose file name the installer registers."""
    held = env.get("CLAUDE_CODE_SHELL_PREFIX", "")
    return os.path.basename(held.replace("\\", "/")) == "tool-containment.py"


def session_token(session_id):
    """The session id as `tool-containment.py` spells it in a unit name, or "" where there is none."""
    return re.sub(r"[^A-Za-z0-9-]", "", session_id or "")[:64]


def session_scopes(session_ids, runner=_systemctl):
    """The tool-call scopes these sessions left loaded, or None when systemd cannot be asked.

    Only tool-call scopes match, since the prefix places nothing else in a scope. Several ids are
    accepted because the hook payload and the environment can name different ones, and the prefix
    names a scope from the environment.
    """
    tokens = sorted({t for t in map(session_token, session_ids) if t})
    if not tokens:
        return []
    code, out = runner(
        "list-units",
        "--type=scope",
        "--all",
        "--plain",
        "--no-legend",
        "--no-pager",
        *(f"{_TOOL_UNIT}-{t}-*.scope" for t in tokens),
    )
    if code != 0:
        return None
    return [line.split()[0] for line in out.splitlines() if line.split()]


def unit_props(units, props, runner=_systemctl):
    """{unit: {property: value}} for each unit `systemctl show` answers for, empty where it cannot run."""
    if not units:
        return {}
    code, out = runner("show", "--property=Id", *(f"--property={p}" for p in props), "--", *units)
    found = {}
    # One block per unit, blank-line separated, with its properties in systemd's order rather than the order asked.
    for block in (out or "").split("\n\n") if code == 0 else []:
        values = dict(line.partition("=")[::2] for line in block.splitlines())
        if values.get("Id"):
            found[values["Id"]] = values
    return found


def scope_pids(units, runner=_systemctl, read=None):
    """{unit: [pid, ...]} read from each scope's cgroup, empty for a scope whose cgroup cannot be read."""

    def _read(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    read = read or _read
    groups = {
        u: v["ControlGroup"]
        for u, v in unit_props(units, ["ControlGroup"], runner).items()
        if v.get("ControlGroup")
    }
    return {
        u: [int(p) for p in read(f"/sys/fs/cgroup{groups[u]}/cgroup.procs").split() if p.isdigit()]
        if u in groups
        else []
        for u in units
    }


def _scope_lines(units, pids, table, label=None):
    """Two report lines per scope at most: the scope, then up to three of its root processes."""
    lines = []
    for u in units:
        members = pids.get(u, [])
        roots = [p for p in members if table.get(p, (None,))[0] not in members] or members
        tail = f"  {label(u)}" if label else ""
        lines.append(f"  {u}  {len(members)} process{'' if len(members) == 1 else 'es'}{tail}")
        for p in roots[:3]:
            if p in table:
                lines.append(f"    pid {p}  age {_age(table[p][2])}  {table[p][3][:140]}")
    return lines


def reap(session_ids, table, runner=_systemctl, read=None, manager_found=True):
    """Stop this session's tool-call scopes, returning the report, or "" when there were none."""
    units = session_scopes(session_ids, runner)
    if units is None and not manager_found:
        return (
            "agent-safety: this session ran under the containment prefix, but no systemd user "
            "manager was found at its end, so this sweep cannot tell whether any command scope "
            "was started or still runs."
        )
    if units is None:
        return (
            "agent-safety: could not list this session's command scopes, so none was stopped. "
            f"Check with: systemctl --user list-units '{_TOOL_UNIT}-*'"
        )
    if not units:
        return ""
    pids = scope_pids(units, runner, read)
    runner("stop", "--", *units)
    # Re-listed whatever the stop returned, since a scope collected between the list and the stop fails it.
    left = session_scopes(session_ids, runner)
    left = units if left is None else [u for u in left if u in units]
    stopped = [u for u in units if u not in left]
    lines = []
    if stopped:
        noun = "scope" if len(stopped) == 1 else "scopes"
        lines.append(
            f"agent-safety: stopped {len(stopped)} command {noun} this session left running."
        )
    lines += _scope_lines(stopped, pids, table)
    if left:
        lines.append(
            f"agent-safety: {len(left)} command scope(s) did not stop. End them with:\n"
            f"  systemctl --user kill --signal=SIGKILL {' '.join(left)}"
        )
    return "\n".join(lines)


def tree_slice(root):
    """The slice `tool-containment.py` names for the tree rooted at session token `root`, or None."""
    name = (root or "").replace("-", "")
    return f"{_TREE_UNIT}-{name}.slice" if name else None


def scope_session(unit):
    """The session token a tool-call scope's name carries, before its `<pid>-<random>` suffix."""
    body = unit.removeprefix(f"{_TOOL_UNIT}-").removesuffix(".scope")
    return body.rsplit("-", 2)[0] if body.count("-") >= 2 else body


def scope_agent(props):
    """The agent pid a scope's description records, or None for a scope that records none."""
    found = _AGENT_RECORD.search(props.get("Description", ""))
    return int(found.group(1)) if found else None


def orphaned(units, props, table):
    """(gone, unrecorded): the scopes whose recorded agent no longer runs, and those recording none.

    The agent is the process that ran the tool call, so a scope it no longer outlives belongs to a
    session that has ended. This is a test on the agent rather than on each process's ancestry, since a
    command detached on purpose loses its agent ancestor while its session is still running.
    """
    gone, unrecorded = [], []
    for u in units:
        agent = scope_agent(props.get(u, {}))
        if agent is None:
            unrecorded.append(u)
        elif agent not in table or not is_agent(table[agent][3]):
            gone.append(u)
    return gone, unrecorded


def _tool_scopes(runner):
    """(units, props) for every tool-call scope the manager holds, with its slice and description, or None."""
    code, out = runner(
        "list-units",
        "--type=scope",
        "--all",
        "--plain",
        "--no-legend",
        "--no-pager",
        f"{_TOOL_UNIT}-*.scope",
    )
    if code != 0:
        return None
    units = sorted(line.split()[0] for line in out.splitlines() if line.split())
    return units, unit_props(units, ["Slice", "Description"], runner)


def _tree_members(name, runner):
    """(members, slice props, scope props) for a tree's slice, or None where it cannot be listed.

    A slice names no property listing the scopes in it, so membership is read from each scope's own
    `Slice`. The tree's slice is stopped only when this finds it empty, and stopping a slice ends
    everything in it, so a failure to list is never read as empty.
    """
    scopes = _tool_scopes(runner)
    code, out = runner("show", "--property=ActiveState", "--property=DropInPaths", "--", name)
    if scopes is None or code != 0:
        return None
    units, props = scopes
    members = [u for u in units if props.get(u, {}).get("Slice") == name]
    return members, dict(line.partition("=")[::2] for line in out.splitlines()), props


def _gone_label(props):
    """The label naming an orphaned scope's session and the agent it outlived."""
    return lambda u: f"session {scope_session(u)}, agent pid {scope_agent(props[u])} gone"


def reap_tree(session_ids, env, table, runner=_systemctl, read=None):
    """Stop the scopes this session's tree holds for sessions that have ended, returning the report.

    The tree is the inherited root's, or this session's own where it is the root. Only the root stops
    the slice itself, and only once nothing is left in it, since a nested session ending says nothing
    about the rest of the tree. `table` is None where the process table could not be read, which says
    nothing about which agents run, so nothing is stopped then.
    """
    own = sorted({t for t in map(session_token, session_ids) if t})
    inherited = session_token(env.get(ROOT_VAR, ""))
    roots = [inherited] if inherited else own
    if roots and table is None:
        return (
            "agent-safety: could not read the process table, so no command scope in this session's "
            f"tree was checked. Check with: python3 {os.path.abspath(__file__)} --orphans"
        )
    lines = []
    for root in roots:
        name = tree_slice(root)
        if name is None:
            continue
        listed = _tree_members(name, runner)
        if listed is None:
            lines.append(
                f"agent-safety: could not list the command scopes in this session's tree {name}, "
                f"so none was stopped. Check with: python3 {os.path.abspath(__file__)} --orphans"
            )
            continue
        members, slice_props, props = listed
        gone, _unrecorded = orphaned(members, props, table)
        if gone:
            pids = scope_pids(gone, runner, read)
            runner("stop", "--", *gone)
            # Re-listed whatever the stop returned, since a scope collected between the list and the stop fails it.
            after = _tree_members(name, runner)
            left = gone if after is None else [u for u in after[0] if u in gone]
            stopped = [u for u in gone if u not in left]
            if stopped:
                noun = "scope" if len(stopped) == 1 else "scopes"
                lines.append(
                    f"agent-safety: stopped {len(stopped)} command {noun} in this session's tree "
                    "left by a session that had ended without stopping them."
                )
            lines += _scope_lines(stopped, pids, table, _gone_label(props))
            if left:
                lines.append(
                    f"agent-safety: {len(left)} orphaned command scope(s) did not stop. End them with:\n"
                    f"  systemctl --user kill --signal=SIGKILL {' '.join(left)}"
                )
            members = members if after is None else after[0]
        # Only the root ends the tree, and only an empty one, since a scope whose agent runs is a live session's.
        if (inherited and inherited not in own) or members:
            continue
        if slice_props.get("ActiveState") == "active" and runner("stop", "--", name)[0] != 0:
            lines.append(f"agent-safety: could not stop this session's tree {name}.")
        if slice_props.get("DropInPaths") and runner("revert", "--", name)[0] != 0:
            lines.append(
                f"agent-safety: could not remove the ceiling put on this session's tree {name}. "
                f"Remove it with: systemctl --user revert {name}"
            )
    return "\n".join(lines)


def orphan_report(table, runner=_systemctl):
    """(exit code, report) for `--orphans`: every tool-call scope on the machine whose agent is gone.

    Read-only, for a session that ended with no sweep. 0 is a clean machine, 1 a finding, 2 a failure
    to ask, kept apart so a report that could not be made never reads as a clean one.
    """
    if table is None:
        return 2, "agent-safety: could not read the process table, so no command scope was checked."
    scopes = _tool_scopes(runner)
    if scopes is None:
        return 2, "agent-safety: could not list command scopes, so none was checked."
    units, props = scopes
    gone, unrecorded = orphaned(units, props, table)
    if not gone and not unrecorded:
        return 0, f"agent-safety: none of {len(units)} command scope(s) outlived its session."
    pids = scope_pids(gone + unrecorded, runner)
    lines = []
    if gone:
        one = len(gone) == 1
        lines.append(
            f"agent-safety: {len(gone)} command {'scope' if one else 'scopes'} outlived the "
            f"session that started {'it' if one else 'them'}, since the agent "
            f"{'it records' if one else 'each records'} is gone."
        )
        by_tree = {}
        for u in gone:
            by_tree.setdefault(props.get(u, {}).get("Slice") or "(no tree)", []).append(u)
        for tree in sorted(by_tree):
            lines.append(f"tree {tree}")
            lines += _scope_lines(by_tree[tree], pids, table, _gone_label(props))
        lines += ["End them with:", f"  systemctl --user stop {' '.join(gone)}"]
    if unrecorded:
        lines.append(
            f"agent-safety: {len(unrecorded)} command scope(s) record no agent, as one an older "
            "prefix started does not, so whether their session has ended is unknown here:"
        )
        lines += _scope_lines(unrecorded, pids, table, lambda u: f"session {scope_session(u)}")
    return 1, "\n".join(lines)


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        payload = {}
    # Windows has no session test at all, and a BSD `ps` carries neither `etimes` nor a decimal
    # `sess`, so on both the hook is inert rather than printing a failure at every session end.
    if os.name != "posix" or sys.platform == "darwin":
        return 0
    held = payload.get("session_id") if isinstance(payload, dict) else None
    session_ids = [
        held if isinstance(held, str) else "",
        os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
    ]
    reaped = ""
    runtime = manager_runtime(os.environ)
    if should_reap(runtime, os.environ):

        def runner(*args):
            return _systemctl(*args, runtime=runtime)

        reaped = reap(
            session_ids, _read_process_table() or {}, runner=runner, manager_found=bool(runtime)
        )
        if runtime:
            # Read after the stop above, which is what ended any nested agent that ran in those scopes.
            tree = reap_tree(session_ids, os.environ, _read_process_table(), runner=runner)
            reaped = "\n".join(t for t in (reaped, tree) if t)
    if reaped:
        print(reaped, file=sys.stderr)
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
        return 2 if reaped else 0
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
    sid = "0a1b-2c3d"
    live = {f"{_TOOL_UNIT}-{sid}-400.scope", f"{_TOOL_UNIT}-{sid}-600.scope"}
    calls = []

    def fake(*args, stop_code=0, sticky=()):
        calls.append(args)
        if args[0] == "list-units":
            return 0, "".join(
                f"{u} loaded active running Claude Code command\n" for u in sorted(live)
            )
        if args[0] == "show":
            return 0, "".join(
                f"ControlGroup=/app.slice/{u}\nId={u}\n\n" for u in args if u.endswith(".scope")
            )
        if args[0] == "stop":
            live.difference_update(u for u in args[2:] if u not in sticky)
            return stop_code, ""
        return 1, ""

    procs = {f"/sys/fs/cgroup/app.slice/{_TOOL_UNIT}-{sid}-400.scope/cgroup.procs": "400\n410\n"}
    reaped = reap([sid], table, runner=fake, read=lambda p: procs.get(p, ""))
    stopped_all = not live
    live.update({f"{_TOOL_UNIT}-{sid}-400.scope"})
    sticky = f"{_TOOL_UNIT}-{sid}-400.scope"
    stuck = reap(
        [sid], table, runner=lambda *a: fake(*a, sticky=(sticky,)), read=lambda p: procs.get(p, "")
    )
    live.update({f"{_TOOL_UNIT}-{sid}-400.scope", f"{_TOOL_UNIT}-{sid}-600.scope"})
    raced = reap(
        [sid], table, runner=lambda *a: fake(*a, stop_code=5), read=lambda p: procs.get(p, "")
    )
    live.clear()

    def has(name):
        return "/usr/bin/" + name

    def socket_in(*dirs):
        return lambda path: any(path == os.path.join(d, "systemd", "private") for d in dirs)

    two_calls = []
    session_scopes(["b", "a", "a", ""], runner=lambda *a: two_calls.append(a) or (0, ""))
    checks += [
        (
            "stopped 2 command scopes" in raced and "did not stop" not in raced,
            "a stop that fails on a scope already collected still reports what the re-list shows",
        ),
        (stopped_all, "every tool-call scope of this session is stopped"),
        (
            calls[0][-1] == f"{_TOOL_UNIT}-{sid}-*.scope",
            "and only this session's tool-call scopes are listed",
        ),
        ("stopped 2 command scopes" in reaped, "the report counts what was stopped"),
        ("2 processes" in reaped and "pid 400" in reaped, "and names each scope's root process"),
        ("pid 410" not in reaped, "but not a process under that root"),
        ("did not stop" in stuck and f"SIGKILL {sticky}" in stuck, "a scope that stays is named"),
        (reap(["", ""], table, runner=fake) == "", "a session with no id stops nothing"),
        (reap([sid], table, runner=fake) == "", "a session that left nothing reports nothing"),
        (
            "could not list" in reap([sid], table, runner=lambda *a: (None, "")),
            "a systemctl that cannot run is reported, never read as nothing to stop",
        ),
        (session_token("a/b c;d") == "abcd", "the unit name is spelled as the prefix spells it"),
        (
            prefix_registered({"CLAUDE_CODE_SHELL_PREFIX": "/h/.claude/hooks/tool-containment.py"})
            and not prefix_registered(
                {"CLAUDE_CODE_SHELL_PREFIX": "/bin/audit-tool-containment.py-x"}
            )
            and not prefix_registered({}),
            "a session under the prefix is swept even with no manager found, so a failed list is reported",
        ),
        (
            should_reap(None, {"CLAUDE_CODE_SHELL_PREFIX": "/h/.claude/hooks/tool-containment.py"})
            and should_reap("/run/user/1000", {})
            and not should_reap(None, {}),
            "the stop phase runs under the prefix or a manager, and a host with neither is skipped",
        ),
        (
            "cannot tell whether any command scope"
            in reap([sid], table, runner=lambda *a: (1, ""), manager_found=False),
            "no manager at session end is reported as unknown, never as a lost manager",
        ),
        (
            manager_runtime({}, which=has, exists=socket_in(), uid=1000) is None,
            "a host with no manager socket anywhere has no manager to ask",
        ),
        (
            manager_runtime(
                {"XDG_RUNTIME_DIR": "/mnt/wslg/runtime-dir"},
                which=has,
                exists=socket_in("/run/user/1000"),
                uid=1000,
            )
            == "/run/user/1000",
            "a runtime directory lacking the socket falls back to the account's standard one",
        ),
        (
            manager_runtime({"XDG_RUNTIME_DIR": "/run/user/1000"}, which=lambda n: None, uid=1000)
            is None,
            "and a host with no systemctl is never asked",
        ),
        (
            len(two_calls) == 1
            and two_calls[0][-2:] == (f"{_TOOL_UNIT}-a-*.scope", f"{_TOOL_UNIT}-b-*.scope"),
            "a payload id and an environment id that differ are both listed, in one call",
        ),
    ]
    # Root session 0a1b-2c3d, whose agent 200 still runs at its own session end, and nested 5e6f-7a8b, whose agent 250 is gone.
    tree = "claude-tree-0a1b2c3d.slice"
    nested = f"{_TOOL_UNIT}-5e6f-7a8b-700-aa.scope"
    deeper = f"{_TOOL_UNIT}-9c0d-710-bb.scope"
    alive = f"{_TOOL_UNIT}-0a1b-2c3d-720-cc.scope"
    reused = f"{_TOOL_UNIT}-5e6f-7a8b-730-dd.scope"
    older = f"{_TOOL_UNIT}-0a1b-2c3d-740-ee.scope"
    outsider = f"{_TOOL_UNIT}-1f2e-750-ff.scope"
    agents = {nested: 250, deeper: 260, alive: 200, reused: 300, outsider: 270}

    def world(members, slice_state="active", outside=(outsider,)):
        state = {"members": set(members), "slice": slice_state, "dropins": True, "calls": []}

        def run(*args):
            state["calls"].append(args)
            if args[0] == "show" and args[-1] == tree:
                return 0, (
                    f"ActiveState={state['slice']}\n"
                    f"DropInPaths={'/run/x/50-TasksMax.conf' if state['dropins'] else ''}\n"
                )
            if args[0] == "show":
                units = [a for a in args if a.endswith(".scope")]
                return 0, "".join(
                    f"Id={u}\nControlGroup=/{tree}/{u}\n"
                    f"Slice={'app.slice' if u == outsider else tree}\nDescription=Claude Code command"
                    + (f", agent pid {agents[u]}" if u in agents else "")
                    + "\n\n"
                    for u in units
                )
            if args[0] == "list-units":
                listed = sorted(state["members"] | set(outside))
                return 0, "".join(f"{u} loaded active running x\n" for u in listed)
            if args[0] == "stop" and args[-1] == tree:
                state["slice"] = "inactive"
                state["members"].clear()
                return 0, ""
            if args[0] == "stop":
                state["members"].difference_update(args[2:])
                return 0, ""
            if args[0] == "revert":
                state["dropins"] = False
                return 0, ""
            return 1, ""

        return state, run

    tree_procs = {f"/sys/fs/cgroup/{tree}/{nested}/cgroup.procs": "800\n810\n"}
    tree_table = _parse_process_table(
        "\n".join(
            rows
            + [
                "300 1 300 50 mcp-server --stdio",
                "800 1 800 40 /bin/bash -c sleep 999 &",
                "810 800 800 40 sleep 999",
            ]
        )
    )
    root_world, root_run = world([nested, deeper])
    root_text = reap_tree([sid], {}, tree_table, root_run, read=lambda p: tree_procs.get(p, ""))
    live_world, live_run = world([nested, alive, older])
    live_text = reap_tree([sid], {}, tree_table, live_run)
    inner_world, inner_run = world([deeper])
    inner_text = reap_tree(["5e6f-7a8b"], {ROOT_VAR: "0a1b-2c3d"}, tree_table, inner_run)
    blind_world, blind_run = world([nested])
    blind_text = reap_tree([sid], {}, None, blind_run)
    unlisted_world, listed_run = world([nested])

    def unlisted_run(*args):
        return (1, "") if args[0] == "list-units" else listed_run(*args)

    empty_world, empty_run = world([], slice_state="inactive")
    empty_world["dropins"] = False
    gone_all = orphaned(
        [nested, alive, reused, older],
        {u: {"Description": f"Claude Code command, agent pid {p}"} for u, p in agents.items()},
        tree_table,
    )
    report_world, report_run = world([nested, deeper, alive, older])
    report_code, report_text = orphan_report(tree_table, report_run)
    clean_code, clean_text = orphan_report(tree_table, world([alive], outside=())[1])
    checks += [
        (
            tree_slice("0a1b-2c3d") == tree and tree_slice("") is None,
            "the tree's slice is named as the prefix names it",
        ),
        (
            scope_session(nested) == "5e6f-7a8b" and scope_session(deeper) == "9c0d",
            "a scope's name gives back the session that ran it",
        ),
        (
            scope_agent({"Description": "Claude Code command, agent pid 250"}) == 250
            and scope_agent({"Description": "Claude Code command"}) is None,
            "a scope's description gives back its agent, or none",
        ),
        (
            gone_all == ([nested, reused], [older]),
            "a scope whose agent is gone or reused has ended, and one recording none is unknown",
        ),
        (
            not root_world["members"]
            and "stopped 2 command scopes in this session's tree" in root_text
            and outsider not in root_text,
            "the root stops every scope its tree holds for a session that ended",
        ),
        (
            "session 5e6f-7a8b, agent pid 250 gone" in root_text and "pid 800" in root_text,
            "and names each one's session, its gone agent, and what it ran",
        ),
        (
            root_world["slice"] == "inactive" and not root_world["dropins"],
            "then stops its empty tree and removes the ceiling put on it",
        ),
        (
            live_world["members"] == {alive, older} and live_world["slice"] == "active",
            "a scope whose agent still runs, or that records none, is left, and so is the tree",
        ),
        (
            "session 5e6f-7a8b" in live_text and alive not in live_text,
            "and only the ended session's scope is reported",
        ),
        (
            not inner_world["members"]
            and inner_world["slice"] == "active"
            and inner_world["dropins"]
            and ("stop", "--", tree) not in inner_world["calls"]
            and "session 9c0d" in inner_text,
            "a nested session clears its tree's ended scopes but never ends the root's tree",
        ),
        (
            blind_world["members"] == {nested}
            and "could not read the process table" in blind_text
            and not any(c[0] == "stop" for c in blind_world["calls"]),
            "an unreadable process table stops nothing, since it cannot say which agents run",
        ),
        (
            "could not list" in reap_tree([sid], {}, tree_table, lambda *a: (1, ""))
            and "could not list" in reap_tree([sid], {}, tree_table, unlisted_run)
            and unlisted_world["slice"] == "active",
            "a tree whose scopes cannot be listed is reported, never read as empty and ended",
        ),
        (
            reap_tree([sid], {}, tree_table, empty_run) == ""
            and not any(c[0] in ("stop", "revert") for c in empty_world["calls"]),
            "a session that never made a tree reports and runs nothing",
        ),
        (
            reap_tree(["", ""], {}, tree_table, lambda *a: 1 / 0) == "",
            "a session with no id has no tree to sweep",
        ),
        (
            report_code == 1
            and f"tree {tree}" in report_text
            and nested in report_text
            and deeper in report_text
            and f"tree app.slice\n  {outsider}" in report_text
            and f"systemctl --user stop {outsider} {nested} {deeper}" in report_text,
            "--orphans names every ended session's scope by its tree, and hands over the stop line",
        ),
        (
            alive not in report_text.split("record no agent")[0]
            and "record no agent" in report_text
            and older in report_text,
            "and lists a scope recording no agent apart, as unknown rather than ended",
        ),
        (
            not any(c[0] in ("stop", "kill", "revert") for c in report_world["calls"]),
            "--orphans stops nothing",
        ),
        (
            clean_code == 0 and "none of 1 command scope" in clean_text,
            "a machine with no ended session's scope says so",
        ),
        (
            orphan_report(tree_table, lambda *a: (1, ""))[0] == 2
            and orphan_report(None, lambda *a: 1 / 0)[0] == 2,
            "--orphans that cannot list, or cannot read the process table, fails rather than reads clean",
        ),
        (
            is_agent("claude --resume")
            and is_agent("node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js")
            and not is_agent("/bin/bash -c cd /x/claude && make")
            and not is_agent("mcp-server --stdio"),
            "an agent is judged as the prefix judges it",
        ),
    ]
    ok = True
    for passed, label in checks:
        if not passed:
            ok = False
        print(f"  {'ok  ' if passed else 'FAIL'} {label}")
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


def orphans_main():
    """`--orphans`: print every command scope on the machine whose session has ended, and stop nothing."""
    runtime = manager_runtime(os.environ)
    if runtime is None:
        print(
            "agent-safety: no systemd user manager is reachable, so no command scope was checked."
        )
        return 2

    def runner(*args):
        return _systemctl(*args, runtime=runtime)

    code, text = orphan_report(_read_process_table(), runner)
    print(text)
    return code


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    sys.exit(orphans_main() if sys.argv[1:] == ["--orphans"] else main())
