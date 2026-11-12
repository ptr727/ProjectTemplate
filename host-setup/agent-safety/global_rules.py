#!/usr/bin/env python3
"""Write the fleet's host-wide agent rules to Codex's and opencode's global instruction files.

The rules are the ones the Claude kit renders into the user CLAUDE.md: the safety block, the fleet
block, and the host-local file. This script writes the same rendered content to each other agent's
global file, so every agent on the host reads one set of rules. Only the header comment differs, as
it names this script. It writes rules and registers no hook. Codex and opencode have no hook
installer yet.

An agent counts as present when its CLI is on PATH or its configuration directory exists. A missing
agent is skipped, so running this never creates a directory for an agent nobody installed.

Usage: python3 global_rules.py                 (write the file of each present agent).
       python3 global_rules.py --agent codex   (one agent only).
       python3 global_rules.py --dry-run       (say what would be written, change nothing).
       python3 global_rules.py --report        (read-only: CURRENT, STALE, or ABSENT per agent).
       CODEX_HOME=/x python3 global_rules.py   (override the Codex directory, for testing).
       XDG_CONFIG_HOME=/x python3 global_rules.py   (override the opencode config root, for testing).
       AGENT_FLEET_LOCAL_INSTRUCTIONS=/x.md python3 global_rules.py   (override the host-local file).
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import os
import pathlib
import shutil
import signal
import sys
import types
from dataclasses import dataclass
from types import FrameType

HERE = pathlib.Path(__file__).resolve().parent
KIT_INSTALL = HERE / "claude" / "install.py"

# What a file written by this script opens with.
# A prior file without it was written by a person or another tool, so it is backed up before it is replaced.
WRITER_MARKER = "<!-- Written by ProjectTemplate host-setup/agent-safety/global_rules.py"

EXIT_SIGINT = 130
EXIT_SIGTERM = 143


@dataclass(frozen=True)
class Agent:
    name: str
    command: str
    config_dir: pathlib.Path
    target: pathlib.Path


def load_kit() -> types.ModuleType:
    """The Claude installer module, loaded by path so its rendering and file writes are reused rather than copied."""
    spec = importlib.util.spec_from_file_location("agent_safety_claude_install", KIT_INSTALL)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {KIT_INSTALL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def codex_dir() -> pathlib.Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return pathlib.Path(override).expanduser().absolute()
    return pathlib.Path.home() / ".codex"


def opencode_dir() -> pathlib.Path:
    # The XDG spec treats a relative value as unset, as the kit's host-local file path does.
    root = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or "").expanduser()
    base = root if root.is_absolute() else pathlib.Path.home() / ".config"
    return base / "opencode"


def agents(which: str) -> list[Agent]:
    every = [
        Agent("codex", "codex", codex_dir(), codex_dir() / "AGENTS.md"),
        Agent("opencode", "opencode", opencode_dir(), opencode_dir() / "AGENTS.md"),
    ]
    return [a for a in every if which in ("all", a.name)]


def present(agent: Agent) -> bool:
    return shutil.which(agent.command) is not None or agent.config_dir.is_dir()


# Said under the header because the blocks below describe Claude Code's hooks as the mechanical layer.
# Neither agent has such a hook, so a session of either would otherwise read a backstop that is not there.
NOTE = (
    "Note for this agent: no hook enforces these rules here yet. "
    "Sentences below that name a hook describe Claude Code only. "
    "The gap is tracked at ptr727/ProjectTemplate#781."
)


def header(local_path: pathlib.Path) -> str:
    comment = (
        f"{WRITER_MARKER}, which rewrites this whole file on every run. "
        f"Put host-specific content in {local_path}, appended below. -->"
    )
    return f"{comment}\n\n{NOTE}"


def read_state(kit: types.ModuleType, agent: Agent, rendered: str) -> tuple[str, bytes | None, str]:
    """The agent's file as (state, raw bytes, reason), the state being CURRENT, STALE, or ABSENT."""
    try:
        raw = kit.read_regular_file(agent.target)
    except FileNotFoundError:
        return "ABSENT", None, ""
    except OSError as e:
        return "STALE", None, f"{agent.target} cannot be read ({e})"
    if raw is None:
        return "STALE", None, f"{agent.target} is not a regular file"
    if kit.normalized(raw.decode("utf-8", errors="replace")) == rendered:
        return "CURRENT", raw, ""
    if raw.startswith(WRITER_MARKER.encode("utf-8")):
        return (
            "STALE",
            raw,
            "this script's own output, and the rules or the host-local file have changed",
        )
    return "STALE", raw, "not written by this script, so a write backs it up first"


def write_agent(
    kit: types.ModuleType, agent: Agent, rendered: str, raw: bytes | None, dry_run: bool
) -> int:
    """Write one agent's file, backing up a prior file this script did not write. Returns 0 or 1."""
    ours = raw is not None and raw.startswith(WRITER_MARKER.encode("utf-8"))
    backup = raw is not None and not ours
    if dry_run:
        verb = "updated" if raw is not None else "written"
        note = ", the prior file backed up first" if backup else ""
        print(f"  {agent.name}: {agent.target} would be {verb}{note}")
        return 0
    # A CRLF file keeps its line endings, as the kit keeps CLAUDE.md's.
    newline = "\r\n" if raw is not None and b"\r\n" in raw else "\n"
    encoded = rendered.replace("\n", newline).encode("utf-8")
    backups: list[pathlib.Path] = []
    try:
        agent.target.parent.mkdir(parents=True, exist_ok=True)
        # Written in place rather than renamed over, so a dotfiles symlink stays a link, as the kit's CLAUDE.md write does.
        kit.write_regular_file(
            agent.target,
            encoded,
            prior=raw,
            before=(lambda: backups.append(kit.write_backup(agent.target, raw)))
            if backup
            else None,
        )
    except OSError as e:
        kept = f" Its prior content is backed up at {backups[0]}." if backups else ""
        sys.stderr.write(f"{agent.name}: {agent.target} could not be written ({e}).{kept}\n")
        return 1
    if backups:
        print(f"  {agent.name}: {agent.target} rewritten, the prior file backed up to {backups[0]}")
    else:
        print(f"  {agent.name}: {agent.target} {'updated' if raw is not None else 'written'}")
    return 0


def run(args: argparse.Namespace) -> int:
    # The kit module is loaded only once an agent is present, so a host with none runs this on any python3.
    wanted = [agent for agent in agents(args.agent) if present(agent)]
    for agent in agents(args.agent):
        if agent not in wanted:
            print(f"  {agent.name}: not installed, skipped")
    if not wanted:
        return 0
    # A capability probe rather than a version tuple, which the linter reads as dead code under the py313 target.
    if not hasattr(datetime, "UTC"):
        sys.stderr.write(
            "Python 3.11+ is needed to write the rules for an installed agent. Run this with a newer python3.\n"
        )
        return 1
    kit = load_kit()
    local_path = kit.local_instructions_path()
    local_text, problem = kit.read_local_instructions(local_path)
    if problem:
        sys.stderr.write(f"Nothing was written: {problem}.\n")
        return 1
    rendered = kit.render_instructions(local_path, local_text, header=header(local_path))

    status = 0
    for agent in wanted:
        state, raw, reason = read_state(kit, agent, rendered)
        if args.report:
            print(f"  {agent.name}: {state} {agent.target}" + (f" ({reason})" if reason else ""))
            if state != "CURRENT":
                status = 1
        elif state == "CURRENT":
            print(f"  {agent.name}: {agent.target} already current")
        elif raw is None and state == "STALE":
            # Not a regular file, or unreadable.
            # A write would fail or swallow, so it is refused.
            sys.stderr.write(f"{agent.name}: {reason}. Move it aside and re-run.\n")
            status = 1
        elif write_agent(kit, agent, rendered, raw, args.dry_run):
            status = 1
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write the fleet's host-wide agent rules to Codex's and opencode's global files."
    )
    parser.add_argument(
        "--agent",
        choices=("codex", "opencode", "all"),
        default="all",
        help="which agent to write or report, default all",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--report",
        action="store_true",
        help="read-only: CURRENT, STALE, or ABSENT per present agent, non-zero unless all are CURRENT",
    )
    mode.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="say what would be written, change nothing",
    )
    return parser


def main() -> int:
    received: list[int] = []

    # SIGTERM takes the path SIGINT takes, so one handler below releases what the run holds and reports it.
    def on_sigterm(signum: int, _frame: FrameType | None) -> None:
        received.append(signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)
    args = build_parser().parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        name = "SIGTERM" if received else "SIGINT"
        sys.stderr.write(f"Interrupted by {name}, no further file was written.\n")
        return EXIT_SIGTERM if received else EXIT_SIGINT


if __name__ == "__main__":
    sys.exit(main())
