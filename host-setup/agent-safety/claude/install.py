#!/usr/bin/env python3
"""Install the agent host-safety kit for the current user account. Cross-platform, idempotent.

Deploys the PreToolUse hook and the SessionEnd stray-process sweep, registers both in the user
settings.json, merges the permission rules this kit owns into the same file, adds the safety rules to
the user CLAUDE.md (marker-delimited so re-runs update in place), and self-tests each hook before
registering it.
The bash and PowerShell wrappers both call this, so every OS runs one tested code path.

Every run records a stamp at ~/.claude/agent-safety-stamp.json naming the machine, what was
installed, and the hub commit it came from, so a fleet rollout can be tracked from the hosts
rather than from memory. `--report` reads that stamp against this checkout and answers whether
the machine is current, without changing anything.

Usage: python3 install.py            (installs to ~/.claude)
       python3 install.py --report   (read-only: is this machine current?)
       CLAUDE_HOME=/x python3 install.py   (override target, for testing)
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

# Each hook's file name, and the substring identifying its registration in settings.json.
# Named once, since a registration written under one spelling and searched for under another is reported absent forever.
# The guard was spelled by hand on the search side and by position on the deploy side, which is that same drift one rename away.
GUARD_NAME = "gh-write-guard.py"
GUARD_STEM = "gh-write-guard"
SWEEP_NAME = "stray-process-sweep.py"
SWEEP_STEM = "stray-process-sweep"

# The hook files this kit copies into ~/.claude/hooks, in deploy order.
# Named here rather than spelled inside `main`, since a test scraping `main` for a literal path goes silent when the copy is refactored.
# That silence reads as a pass.
DEPLOYED_HOOKS = (GUARD_NAME, SWEEP_NAME)

# A SessionEnd hook's own budget is 1.5 seconds, raised to the highest per-hook timeout the settings declare.
# The sweep reads one process table, so this is headroom for a loaded machine rather than a duration it uses.
SWEEP_TIMEOUT_SECONDS = 10

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

# The files whose bytes this kit actually places on a machine, the hook first and then each block.
# Derived rather than listed, so a block added above enters the digest without a second edit.
# Written out, this list and the block list drifted apart silently and the digest stopped covering a file.
# The digest is taken over these rather than over the commit, since it is the content that runs.
# A clean commit and a dirty checkout install different bytes while reporting the same SHA.
PAYLOAD_FILES = DEPLOYED_HOOKS + tuple(filename for _, filename in CLAUDE_MD_BLOCKS)

# Distinguishes an absent key from one holding an explicit null, which `dict.get` reports alike.
# The two need different answers, since a gap is filled and a null is a settings error.
MISSING = object()


def hook_command(launcher, path):
    """The `command` string a registered hook carries, spelled once for writer and reader alike."""
    return f'"{launcher}" "{path}"'


def runs_hook(command, path):
    """Whether `command` names the hook deployed at `path`, as its own argument.

    The path is compared and the launcher is not. Matching the name alone let an unrelated
    `echo stray-process-sweep` count as a registration, while matching the whole command string
    reported a working machine stale, since `hook_launcher()` resolves at check time and a machine
    installed under a different PATH carries the other spelling. The path is what the installer
    writes and never drifts, so it is the half worth comparing.

    The quoting around it is not compared either. This installer writes the path in double quotes,
    and a registration written by hand or through the `/hooks` UI runs the same file bare or in
    single quotes, so requiring one spelling reported those as naming a hook they do not run.

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


def matcher_sees_bash(matcher):
    """Whether a PreToolUse matcher group receives a Bash call.

    An absent or empty matcher runs on every tool and `*` is the all-tools spelling, so neither is
    a defect. A matcher is otherwise a regular expression, which makes `Bash|Task` as good as
    `Bash`. Requiring the exact string reported all three of those working shapes as broken.
    """
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.fullmatch(matcher, "Bash") is not None
    except re.error:
        return False


def matcher_covers_every_exit_reason(matcher):
    """Whether a SessionEnd matcher has a shape known to run on every exit reason, absent, empty, or `*`.

    A SessionEnd matcher filters by exit reason the way a PreToolUse matcher filters by tool: an
    absent or empty matcher runs on every reason and `*` is the all-reasons spelling, so none of
    the three is a defect. Unlike `matcher_sees_bash`, there is no single reason to test regex
    membership against, since the sweep must fire on every exit reason rather than one particular
    one, so this classifies the shape rather than evaluating a pattern.
    """
    return matcher is None or matcher in ("", "*")


def owns(entry, prefix):
    """Whether an allow rule names the script the prefix identifies, rather than a longer path.

    The prefix ends at the script name, so a bare `startswith` also claims `pr_review.py-custom`,
    and dropping that would delete a hand-written rule for a different script. What separates the
    two is the character after the name: a rule that invokes this script continues with a rule-syntax
    delimiter, where a different script continues with more of its own path.
    """
    return entry.startswith(prefix) and entry[len(prefix) : len(prefix) + 1] in (":", " ", ")")


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
    # The review loop's reply and resolve, the one write in that loop an agent performs.
    # Driving it by hand needs a raw GraphQL mutation carrying a node id, which is the shape to avoid.
    # The rule decides which command skips a prompt, and it bounds no checkout, since it matches the text.
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

    A dirty tree is reported rather than hidden: the SHA still names a commit, but the bytes
    installed are not that commit's, and a stamp that claims otherwise is the thing this exists
    to prevent. A checkout that is not a git tree at all (an extracted tarball) says so.
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

    sha = git("rev-parse", "HEAD")
    if not sha:
        return {"vcs": "none"}
    ref = {"vcs": "git", "commit": sha}
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        ref["branch"] = branch
    status = git("status", "--porcelain", "--", *PAYLOAD_FILES)
    ref["dirty"] = bool(status)
    return ref


def normalized(data):
    """Line endings reduced to newlines, covering CRLF and a bare CR.

    One helper rather than a replace at each site. Both digests have to normalize identically or a
    machine drifts on nothing, and a site that handled CRLF while missing CR did exactly that: the
    installer reads a snippet in text mode, so a bare CR arrives as a newline and installs as one,
    while a digest that left it alone reported the machine STALE against its own content.
    """
    if isinstance(data, bytes):
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data.replace("\r\n", "\n").replace("\r", "\n")


def payload_digest():
    """One digest over the content this kit installs, normalized the way the installer writes it.

    Over raw bytes this reported drift a reinstall could not clear: the snippets are embedded with
    `.strip()`, so a trailing newline moved the digest while the installed block stayed identical,
    and the machine was told to re-run something that would write the same file. Line endings
    normalize for the same reason.

    Fixed order because a set of files has none, and a digest that depends on directory listing
    order reports drift on a machine where nothing changed. The order matches the one
    `installed_digest` reads, so the two are directly comparable: the guard, the sweep, then each block.
    """
    h = hashlib.sha256()
    for name in PAYLOAD_FILES:
        raw = normalized((HERE / name).read_bytes())
        # A snippet is embedded stripped, so trailing whitespace is not installed content.
        # The hook is copied byte for byte, so nothing about it is stripped.
        if name.endswith(".md"):
            raw = raw.decode("utf-8").strip().encode("utf-8")
        h.update(raw)
    return h.hexdigest()[:16]


def blocks_present(claude_md):
    """The marker version of each block actually in CLAUDE.md, by name.

    Read from the file rather than from what the installer meant to write, since the question the
    stamp answers is what is on the machine.
    """
    if not claude_md.exists():
        return {}
    text = claude_md.read_text(encoding="utf-8", errors="replace")
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
    CLAUDE.md records the same empty block set it reads, so the stamp and the file agree and the
    corruption reads as a match. Two wrong answers agreeing is the failure this exists to catch.
    """
    if not claude_md.is_file():
        return []
    text = claude_md.read_text(encoding="utf-8", errors="replace")
    valid = blocks_present(claude_md)
    out = []
    for marker in BLOCK_MARKERS:
        if re.search(rf"<!-- {marker} v\d+ (?:start|end) -->", text) and marker not in valid:
            out.append(f"the {marker} markers in CLAUDE.md are duplicated or incomplete")
    return out


def installed_digest(claude_home):
    """A digest over the bytes actually on this machine, or None where the kit is not fully there.

    Markers and versions answer whether a block is present, and nothing about its content, so a
    block edited between its own markers reports current under a presence check. The hook is not
    marker-delimited at all, so a modified or deleted one is invisible the same way.

    Line endings are normalized first: CLAUDE.md keeps whatever endings it had, and a machine that
    holds identical text with CRLF is current rather than drifted.
    """
    # Read from `DEPLOYED_HOOKS` rather than by name, since naming them here is the drift that constant closes.
    # A hook added to the deploy list and not to this one installs and is never covered by the currentness digest.
    deployed = [claude_home / "hooks" / name for name in DEPLOYED_HOOKS]
    claude_md = claude_home / "CLAUDE.md"
    if not all(f.is_file() for f in deployed) or not claude_md.is_file():
        return None
    h = hashlib.sha256()
    for f in deployed:
        h.update(normalized(f.read_bytes()))
    text = normalized(claude_md.read_text(encoding="utf-8", errors="replace"))
    for marker in BLOCK_MARKERS:
        found = re.search(
            rf"<!-- {marker} v\d+ start -->.*?<!-- {marker} v\d+ end -->", text, re.DOTALL
        )
        if not found:
            return None
        h.update(found.group(0).encode("utf-8"))
    return h.hexdigest()[:16]


def build_stamp(claude_home, installed):
    """The record written to the machine after an install, or computed live for a report."""
    return {
        "stampVersion": STAMP_VERSION,
        "host": host_facts(),
        "source": source_ref(),
        "payloadDigest": payload_digest(),
        "installedDigest": installed_digest(claude_home),
        "blocks": blocks_present(claude_home / "CLAUDE.md"),
        "installedUtc": installed,
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
    settings = claude_home / "settings.json"
    if not settings.is_file():
        return ["settings.json is missing, so the hook is not registered"]
    try:
        data = json.loads(settings.read_text(encoding="utf-8") or "{}")
    # ValueError rather than JSONDecodeError, since it also covers UnicodeDecodeError.
    # A partially written or non-UTF-8 file raises that before the JSON parser is ever reached.
    except (ValueError, OSError) as e:
        return [f"settings.json cannot be read ({e})"]
    if not isinstance(data, dict):
        return ["settings.json does not hold an object at its root"]
    out = []

    def report(note):
        """Append `note` once, since one defect a group carries is not two defects when it holds two entries."""
        if note not in out:
            out.append(note)

    def event_groups(event):
        """The matcher groups under `event`, or None when the settings shape cannot be read.

        A wrong shape is reported as itself rather than counted as zero. Iterating a dict yields
        its keys and a string yields its characters, so the loops below would find no registration
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

    groups = event_groups("PreToolUse")
    # Counted apart from the registrations, since an entry with a problem is still an entry.
    # Counting only the sound ones added "the guard never runs" under every problem reported here.
    # That line is false, and it sends a reader to the wrong fix.
    named = 0
    registered = 0
    guard_path = claude_home / "hooks" / GUARD_NAME
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if not isinstance(hook, dict) or GUARD_STEM not in str(hook.get("command", "")):
                continue
            named += 1
            if not runs_hook(hook.get("command"), guard_path) or hook.get("type") != "command":
                out.append(
                    "a PreToolUse entry names the guard but does not run the deployed one "
                    f"({hook.get('type')!r} running {hook.get('command')!r})"
                )
                continue
            if not matcher_sees_bash(group.get("matcher")):
                report(
                    f"a PreToolUse group registers the guard under matcher "
                    f"{group.get('matcher')!r}, which no Bash call matches, so that group never "
                    "fires"
                )
                continue
            registered += 1
    if groups is None:
        pass  # the shape error is already reported, and a count from it would be meaningless
    elif named == 0:
        out.append(
            "the PreToolUse hook is not registered in settings.json, so the guard never runs"
        )
    elif registered > 1:
        out.append(
            f"the PreToolUse hook is registered {registered} times, so it runs more than once"
        )
    ends = event_groups("SessionEnd")
    sweeps_named = 0
    swept = 0
    for group in ends or []:
        if not isinstance(group, dict):
            continue
        sweep_path = claude_home / "hooks" / SWEEP_NAME
        for hook in group.get("hooks") or []:
            if not isinstance(hook, dict) or SWEEP_STEM not in str(hook.get("command", "")):
                continue
            sweeps_named += 1
            if not runs_hook(hook.get("command"), sweep_path) or hook.get("type") != "command":
                out.append(
                    "a SessionEnd entry names the sweep but does not run the deployed one "
                    f"({hook.get('type')!r} running {hook.get('command')!r})"
                )
                continue
            # A longer budget than this installer writes is not a defect, so only a shorter one is reported.
            # The event's own default is 1.5s, which is what the sweep needs more than.
            timeout = hook.get("timeout")
            too_short = isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            if not too_short:
                too_short = timeout < SWEEP_TIMEOUT_SECONDS
            if too_short:
                carries = (
                    "carries no timeout"
                    if "timeout" not in hook
                    else f"carries timeout {timeout!r}"
                )
                out.append(
                    f"the SessionEnd sweep {carries} where this installer writes "
                    f"{SWEEP_TIMEOUT_SECONDS}, too little for a `ps` on a loaded machine"
                )
                continue
            swept += 1
            # Any matcher other than absent, empty, or `*` may filter some exit reasons out.
            # Counting it as registered reports a machine current while the sweep may never fire on an ordinary exit.
            if not matcher_covers_every_exit_reason(group.get("matcher")):
                report(
                    f"the SessionEnd sweep is registered under a matcher "
                    f"({group.get('matcher')!r}) other than absent, empty, or `*`, so it may not run on every exit reason"
                )
    if ends is None:
        pass  # likewise reported as a shape error above
    elif sweeps_named == 0:
        out.append(
            "the SessionEnd sweep is not registered in settings.json, so a surviving shell is "
            "never reported"
        )
    elif swept > 1:
        out.append(f"the SessionEnd sweep is registered {swept} times, so it runs more than once")
    allow = (
        data.get("permissions", {}).get("allow")
        if isinstance(data.get("permissions"), dict)
        else None
    )
    for _, rule in MANAGED_PERMISSIONS:
        if not isinstance(allow, list) or rule not in allow:
            out.append(f"the permission rule {rule} is absent from settings.json")
    return out


def stamp_line(stamp):
    """One line naming the machine and what it carries, short enough to paste into a checklist.

    Every read is total. The caller validates the shape first, and this stays printable anyway,
    since a formatter that raises turns a verdict about a broken stamp into a traceback.
    """
    host = stamp.get("host") or {}
    src = stamp.get("source") or {}
    where = (
        host.get("distro") or f"{host.get('system', 'unknown')} {host.get('release', '')}".strip()
    )
    if host.get("wsl"):
        where += " (WSL)"
    commit = str(src.get("commit", "unknown"))[:7] + ("-dirty" if src.get("dirty") else "")
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

    Compared on the payload digest rather than on the commit, because a machine installed from an
    older commit whose kit bytes never changed is current, and reporting it as stale sends someone
    to re-run an installer that would write the same file.
    """
    path = claude_home / "agent-safety-stamp.json"
    current = payload_digest()
    print(f"This checkout: payload {current}, hub {source_ref().get('commit', 'unknown')[:7]}")
    if not path.exists():
        print(f"NOT INSTALLED: no stamp at {path}")
        print("  Run the installer with no arguments to install and stamp this machine.")
        return 2
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))
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
            "the deployed hook or CLAUDE.md is missing, so the kit is not fully installed"
        )
    elif live_installed != current:
        problems.append("the installed content differs from what this checkout would write")
    # Correct bytes on disk are not a running guard, so the wiring is checked as well.
    problems.extend(registration_problems(claude_home))
    # Read from the file rather than compared against the stamp.
    # An install onto a corrupted file writes the corruption into the stamp, and the two then agree.
    problems.extend(marker_corruption(claude_home / "CLAUDE.md"))
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
        print("  Re-run the installer with no arguments. It is idempotent.")
        return 1
    print("CURRENT: this machine matches this checkout.")
    return 0


def main():
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
    settings = claude_home / "settings.json"
    claude_md = claude_home / "CLAUDE.md"

    # Reported before anything is created, so a report on an uninstalled machine does not install it.
    if args.report:
        return report(claude_home)

    print(f"Installing agent host-safety kit into: {claude_home}")
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # 1. Stage every hook beside its live path, self-test each staged copy, and only then replace.
    # Copying onto the live path first and testing afterwards left a failed self-test's broken copy installed.
    # The existing registration still pointed at it, so an install that refused to register disabled the guard.
    # Named per process, since two installs sharing one staged path clobber each other's copy.
    # A kill between staging and the replace still leaves one behind, which nothing here can prevent.
    staged = []
    for src_name in DEPLOYED_HOOKS:
        tmp = hooks_dir / f"{src_name}.{os.getpid()}.staged"
        staged.append((src_name, tmp))
        try:
            shutil.copyfile(HERE / src_name, tmp)
        except OSError as e:
            for _n, f in staged:
                f.unlink(missing_ok=True)
            sys.stderr.write(f"staging {src_name} failed ({e}); nothing was replaced.\n")
            return 1
        try:
            os.chmod(tmp, 0o755)
        except OSError:
            pass
        r = subprocess.run(
            [sys.executable, str(tmp), "--selftest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if r.returncode != 0:
            for _n, f in staged:
                f.unlink(missing_ok=True)
            sys.stderr.write(
                f"{src_name} self-test FAILED; nothing was replaced.\n" + r.stdout + r.stderr
            )
            return 1
        print(f"  {src_name} self-test: PASS")
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

    # 2. Register the hook command in settings.json under exactly one PreToolUse/Bash group.
    launcher = hook_launcher()
    # Quote the launcher too: the sys.executable fallback can contain spaces (e.g. C:\Program Files\...).
    hook_cmd = hook_command(launcher, hook_dst)
    # Read into a variable rather than twice off disk, once to test for content and once to parse.
    # Two reads can also disagree, since another process may write between them.
    data = {}
    raw = settings.read_text(encoding="utf-8") if settings.exists() else ""
    if raw.strip():
        try:
            data = json.loads(raw)
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
            "is required. Fix or remove it, then re-run. This file is unchanged, so the hook is "
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
            "Fix or remove that key, then re-run. This file is unchanged, so the hook is deployed "
            "but not registered.\n"
        )

    for path, want in (
        ("hooks", dict),
        ("hooks/PreToolUse", list),
        ("hooks/SessionEnd", list),
        ("permissions", dict),
        ("permissions/allow", list),
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

    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    # Strip the hook from every existing group first, so a re-run leaves no duplicate behind.
    # That matters when settings.json already carries more than one Bash group.
    # Then register it in a single Bash group.
    for g in pre:
        hooks_list = g.get("hooks")
        if isinstance(hooks_list, list):
            hooks_list[:] = [h for h in hooks_list if GUARD_STEM not in str(h.get("command", ""))]
    group = next((g for g in pre if g.get("matcher") == "Bash"), None)
    if group is None:
        group = {"matcher": "Bash", "hooks": []}
        pre.append(group)
    group.setdefault("hooks", []).append({"type": "command", "command": hook_cmd})
    done = ["PreToolUse/Bash hook registered"]

    # Step 2b registers the SessionEnd sweep the same strip-then-register way as the guard above.
    # The chosen or newly created group covers every exit reason, so the sweep fires on all of them.
    ends = data.setdefault("hooks", {}).setdefault("SessionEnd", [])
    for g in ends:
        hooks_list = g.get("hooks")
        if isinstance(hooks_list, list):
            hooks_list[:] = [h for h in hooks_list if SWEEP_STEM not in str(h.get("command", ""))]
    end_group = next((g for g in ends if matcher_covers_every_exit_reason(g.get("matcher"))), None)
    if end_group is None:
        end_group = {"hooks": []}
        ends.append(end_group)
    end_group.setdefault("hooks", []).append(
        {
            "type": "command",
            "command": hook_command(launcher, sweep_dst),
            "timeout": SWEEP_TIMEOUT_SECONDS,
        }
    )
    done.append("SessionEnd sweep registered")

    # 3. Permission rules, merged under the prefixes this installer owns.
    # The strip-then-register shape is the hook registration's above, applied to a flat list.
    # Written in the same pass as the hook, so the file is read once and written once.
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    for prefix, rule in MANAGED_PERMISSIONS:
        matched = [a for a in allow if isinstance(a, str) and owns(a, prefix)]
        allow[:] = [a for a in allow if a not in matched] + [rule]
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

    # Reported after the write rather than as each edit is made, since both edits share one write.
    # A line printed before it claims a change that a later failure would leave unmade.
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for line in done:
        print(f"  settings -> {settings} ({line})")

    # 4. CLAUDE.md carries one marker block per snippet, replaced where present and appended where not.
    # The two blocks install and update independently, so one can change without rewriting the other.
    # The safety block states restrictions only.
    # The fleet block enables, so it stays separate from a block whose own text says nothing in it widens a permission.
    # Preserve CLAUDE.md's existing line endings: work in \n internally, write back with its own ending.
    if claude_md.exists():
        raw = claude_md.read_bytes()
        newline = "\r\n" if b"\r\n" in raw else "\n"
        existing = normalized(raw.decode("utf-8"))
    else:
        newline, existing = "\n", ""
    for marker, filename in CLAUDE_MD_BLOCKS:
        snippet = (HERE / filename).read_text(encoding="utf-8").strip()
        block_re = re.compile(
            rf"<!-- {marker} v\d+ start -->.*?<!-- {marker} v\d+ end -->", re.DOTALL
        )
        if block_re.search(existing):
            # Keep the first occurrence and drop any duplicate, rather than rewriting each in place.
            # Substituting every match preserved the duplication, so a file arriving with two blocks kept two.
            # The report's own remedy of re-running could then never clear it.
            written = []

            def once(_match, _snippet=snippet, _written=written):
                _written.append(True)
                return _snippet if len(_written) == 1 else ""

            existing = block_re.sub(once, existing)
            action = (
                "updated"
                if len(written) == 1
                else f"updated, {len(written) - 1} duplicate(s) removed"
            )
        else:
            sep = (
                ""
                if existing == "" or existing.endswith("\n\n")
                else ("\n" if existing.endswith("\n") else "\n\n")
            )
            existing, action = existing + sep + snippet + "\n", "appended"
        print(f"  CLAUDE.md -> {claude_md} ({marker} block {action})")
    claude_md.write_bytes(existing.replace("\n", newline).encode("utf-8"))

    # 5. Stamp the machine, written last so it records a completed install rather than an attempted one.
    # The blocks are read back off disk here, so the stamp reports what CLAUDE.md holds rather than what was intended.
    stamp_path = claude_home / "agent-safety-stamp.json"
    stamp = build_stamp(
        claude_home, datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    stamp_path.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    print(f"  stamp -> {stamp_path}")

    print("\nDone. This machine:")
    print(f"  {stamp_line(stamp)}")
    print("\nRe-check at any time, from a fresh hub checkout, without changing anything:")
    install_path = HERE / "install.py"
    print(f'  {launcher} "{install_path}" --report')
    print("Restart Claude Code sessions on this machine so the hook and CLAUDE.md load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
