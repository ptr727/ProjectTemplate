#!/usr/bin/env python3
"""Install the agent host-safety kit for the current user account. Cross-platform, idempotent.

Deploys the PreToolUse hook, the SessionEnd stray-process sweep, and the tool-containment shell prefix,
registers the two hooks in the user settings.json and, on a host with a systemd user manager, the prefix
in its `env`, merges the permission rules this kit owns into the same file, renders the user
CLAUDE.md whole from the kit's blocks and the host-local instruction file, and self-tests each hook
before registering it.
The bash and PowerShell wrappers both call this, so every OS runs one tested code path.

Every run records a stamp at ~/.claude/agent-safety-stamp.json naming the machine, what was
installed, and the hub commit it came from, so a fleet rollout can be tracked from the hosts
rather than from memory. `--report` reads that stamp against this checkout and answers whether
the machine is current, without changing anything.

Usage: python3 install.py            (installs to ~/.claude)
       python3 install.py --report   (read-only: is this machine current?)
       CLAUDE_HOME=/x python3 install.py   (override target, for testing)
       AGENT_SAFETY_DIRTY_OVERRIDE=0/1 python3 install.py   (force the dirty-checkout signal, for testing)
       AGENT_SAFETY_CONTAINMENT_OVERRIDE=0/1 python3 install.py   (force the containment-capable signal, for testing)
       AGENT_FLEET_LOCAL_INSTRUCTIONS=/x.md python3 install.py   (override the host-local file, for testing)
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
import stat
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

# NAME serves both the writer (DEPLOYED_HOOKS, hook_dst/sweep_dst) and the reader (`names_hook`).
# A rename made on one side and not the other reports the registration absent forever.
# STEM is read by the test suite alone now, as a substring convenience over a real command.
GUARD_NAME = "gh-write-guard.py"
GUARD_STEM = "gh-write-guard"
SWEEP_NAME = "stray-process-sweep.py"
SWEEP_STEM = "stray-process-sweep"
CONTAIN_NAME = "tool-containment.py"

PREFIX_VAR = "CLAUDE_CODE_SHELL_PREFIX"

# The hook files this kit copies into ~/.claude/hooks, in deploy order.
# Named here rather than spelled inside `main`, since a test scraping `main` for a literal path goes silent when the copy is refactored.
# That silence reads as a pass.
DEPLOYED_HOOKS = (GUARD_NAME, SWEEP_NAME, CONTAIN_NAME)

# A SessionEnd hook's own budget is 1.5 seconds, raised to the highest per-hook timeout the settings declare.
# The sweep's own calls are each bounded, two `ps` reads at 5s and four `systemctl` calls at 5s, all inside this.
SWEEP_TIMEOUT_SECONDS = 35

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


def names_hook(command, name):
    """Whether `command` runs a file whose own name is exactly `name` (e.g. GUARD_NAME), at any path.

    Matched with `runs_hook`'s own boundary characters rather than by tokenizing the command.
    A command `runs_hook` already treats as running the deployed hook, such as one wrapped in
    `bash -c "..."`, in `(...)`, or followed by `; true`, carries those wrapping characters
    attached to the path with no separating space, which `shlex.split` folds into the token and
    a plain whitespace split (its own fallback on an unbalanced quote) leaves attached to a quote
    character, so neither reads the trailing path segment as `name` alone.

    Matched as a trailing path segment rather than a bare substring, so a maintainer's own hook
    whose file name merely contains this kit's stem, such as `my-gh-write-guard-audit.sh`
    containing `gh-write-guard`, is not claimed: the character right after the stem there is
    `-`, not one of the boundary characters this match requires.

    A quoted path is read to its closing quote rather than through the same boundary characters,
    since those are literal there: `"/c/Program Files (x86)/hooks/gh-write-guard.py"` is a path
    `runs_hook` accepts whole, and the bare-word boundary class below cannot cross the space or
    the parentheses inside it to reach the deployed name.

    Known gap, not chased further here: an unquoted path whose own directory holds one of the
    bare-word boundary characters, and a handful of shell compositions `runs_hook` itself only
    accepts because it is handed the exact literal path rather than discovering one (a backtick,
    a no-space redirect, a quote escaped inside an outer quote). Closing those needs `runs_hook`
    widened too, which is a design change on its own rather than a local match.
    """
    text = str(command).replace("\\", "/")
    escaped = re.escape(name)
    quoted = rf'"(?:[^"]*/)?{escaped}"' + "|" + rf"'(?:[^']*/)?{escaped}'"
    bare = rf"(?:^|[\s\"'(])(?:[^\s\"'()|&;]*/)?{escaped}(?=$|[\s\"')|&;])"
    return re.search(quoted, text) is not None or re.search(bare, text) is not None


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


def containment_capable(prefix, env=None, system=None, uid=None, controllers=None):
    """(capable, reason): whether this host can run the deployed `prefix` and start a scope for a command.

    Judged at install and at report time alike, so a host that gains or loses its user manager is
    reported against what it can do now. AGENT_SAFETY_CONTAINMENT_OVERRIDE, read as exactly "0" or
    "1", replaces the judgment for a test, the way AGENT_SAFETY_DIRTY_OVERRIDE does for the dirty signal.
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

    Claude Code runs the file itself, through its `env python3` shebang, for every hook command, the
    write guard included. A PreToolUse hook that fails is a non-blocking error, so a prefix that cannot
    run that way, from a missing `python3`, a lost execute bit, or a `noexec` mount, fails the guard open.
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

    A bare path, since Claude Code quotes the whole value as one word, so an interpreter named in front
    of it is read as part of the file name.
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
    return os.path.basename(str(held).replace("\\", "/")) == CONTAIN_NAME


def foreign_prefix(data):
    """The `CLAUDE_CODE_SHELL_PREFIX` value when it names something other than this kit's prefix, else None."""
    env = data.get("env") if isinstance(data, dict) else None
    held = env.get(PREFIX_VAR) if isinstance(env, dict) else None
    return held if held is not None and not kit_prefix(held) else None


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

    sha = git("rev-parse", "HEAD")
    if not sha:
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

    Opened without blocking and judged on the open descriptor, so a FIFO or a device returns None
    rather than hanging the read, and nothing can swap the path between the check and the read.
    Raises OSError where the open itself fails, FileNotFoundError included for a dangling link.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    # Windows refuses to open a directory at all, and a directory is still not a regular file.
    except FileNotFoundError:
        raise
    except OSError:
        if os.path.isdir(path):
            return None
        raise
    # Closed in a finally, so no return or raise after the open leaves the descriptor behind.
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


class NotRegularFile(OSError):
    """A write target that exists and is not a regular file, refused before anything was written."""


class IncompleteWrite(OSError):
    """A write that failed after the file was truncated, so the file no longer holds what it did."""


def write_regular_file(path, data, prior=None, before=None):
    """Replace the contents of `path`, a regular file or absent, with `data`.

    The open is the check: a file that cannot be opened for writing raises before anything changes,
    so `before`, which backs the file up, runs only once the write is known to be possible.
    Opened without blocking and judged on the descriptor, as `read_regular_file` reads, so a FIFO
    or a device is refused rather than hanging or swallowing the write. Written in place rather than
    replaced, so a dotfiles symlink stays a link and its target takes the content.

    Raises OSError where the file is unchanged, and IncompleteWrite where a write failed after the
    truncate and `prior` could not be put back, or where the close after the truncate failed,
    either of which can leave the file holding neither version.
    """
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o666)
    truncated = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(f"{path} is not a regular file")
        if before is not None:
            before()
        truncated = True
        try:
            _replace_contents(fd, data)
        except OSError as e:
            if prior is None:
                raise IncompleteWrite(f"{path} was left incomplete ({e})") from e
            try:
                _replace_contents(fd, prior)
            except OSError:
                raise IncompleteWrite(
                    f"{path} was left incomplete ({e}), and its prior content could not be put back"
                ) from e
            raise
    except BaseException as e:
        try:
            os.close(fd)
        except OSError as close_error:
            if truncated and not isinstance(e, IncompleteWrite):
                raise IncompleteWrite(
                    f"{path} may be incomplete, since closing it failed ({close_error})"
                ) from e
        raise
    # A network filesystem can report a quota or I/O error at the close rather than at the write.
    try:
        os.close(fd)
    except OSError as e:
        raise IncompleteWrite(f"{path} may be incomplete, since closing it failed ({e})") from e


def _replace_contents(fd, data):
    """Truncate the open file and write all of `data`, since one write may take only part of it."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def read_local_instructions(local_path):
    """The host-local text, stripped, and why it cannot be used, or None where it can.

    Checked before anything is installed, so an unusable file stops the run with nothing changed.
    A file carrying this kit's own markers is refused, since appending it would duplicate a block,
    and copying an old backup into the local file is exactly how that happens.
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


def render_instructions(local_path, local_text):
    """The whole global instruction file in newline form: a header, each block, then the local file.

    Rendered whole rather than merged into whatever the file held. Hand-written sections outside the
    blocks were never written or checked, so they restated rules in wording the fleet had since
    changed, and every session on the host read them anyway.
    """
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


def stamped_instructions_digest(claude_home):
    """The digest of the instruction file the last install wrote, or None where none is recorded.

    None covers a missing or unreadable stamp and one written before the field existed. Each of
    those leaves an edit indistinguishable from an earlier render, so a caller treats it as an edit.
    """
    try:
        raw = read_regular_file(claude_home / "agent-safety-stamp.json")
        stamp = None if raw is None else json.loads(raw.decode("utf-8"))
    except (ValueError, OSError):
        return None
    if stamp is None:
        return None
    # A stamp failing its own shape check vouches for nothing, so its digest is not trusted either.
    if stamp_problems(stamp):
        return None
    value = stamp.get("instructionsDigest")
    return value if isinstance(value, str) else None


def write_backup(claude_md, raw):
    """Write `raw` to a new backup beside CLAUDE.md and return its path, never replacing an earlier one.

    Created exclusively rather than checked and then written, so two runs in one second cannot
    both pick the same free name and have the second overwrite the first.
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

    Over raw bytes this reported drift a reinstall could not clear: the snippets are embedded with
    `.strip()`, so a trailing newline moved the digest while the installed block stayed identical,
    and the machine was told to re-run something that would write the same file. Line endings
    normalize for the same reason.

    Fixed order because a set of files has none, and a digest that depends on directory listing
    order reports drift on a machine where nothing changed. The order matches the one
    `installed_digest` reads, so the two are directly comparable: each deployed hook, then each block.
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


def read_claude_md(claude_md):
    """CLAUDE.md's text, or None where it is absent, not a regular file, or unreadable.

    One reader for every caller, so a file none of them can read gives each the same answer rather
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
    CLAUDE.md records the same empty block set it reads, so the stamp and the file agree and the
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
    return h.hexdigest()[:16]


def build_stamp(claude_home, installed, instructions_digest=None):
    """The record written to the machine after an install, or computed live for a report.

    An install passes the digest of what it rendered rather than re-reading the file. A write by
    anything else between the install's write and this read would otherwise be stamped as the
    install's own, and the next run would replace it with no backup.
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
        return ["settings.json is missing, so the hook is not registered"]
    except OSError as e:
        return [f"settings.json cannot be read ({e})"]
    if raw is None:
        return ["settings.json is not a regular file, so the hook is not registered"]
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    # ValueError rather than JSONDecodeError, since it also covers UnicodeDecodeError.
    # A partially written or non-UTF-8 file raises that before the JSON parser is ever reached.
    except ValueError as e:
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
            if not isinstance(hook, dict) or not names_hook(hook.get("command", ""), GUARD_NAME):
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
            if not isinstance(hook, dict) or not names_hook(hook.get("command", ""), SWEEP_NAME):
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
            "a deployed hook or CLAUDE.md is missing or unreadable, so the kit is not fully installed"
        )
    elif live_installed != current:
        problems.append("the installed content differs from what this checkout would write")
    # Correct bytes on disk are not a running guard, so the wiring is checked as well.
    problems.extend(registration_problems(claude_home))
    try:
        settings_raw = read_regular_file(claude_home / "settings.json")
        settings_data = None if settings_raw is None else json.loads(settings_raw.decode("utf-8"))
    except (ValueError, OSError):
        settings_data = None
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
    # Read from the file rather than compared against the stamp.
    # An install onto a corrupted file writes the corruption into the stamp, and the two then agree.
    problems.extend(marker_corruption(claude_home / "CLAUDE.md"))
    # The whole file is compared too, since content outside the blocks is drift no block check sees.
    # The stamp's digest of the file it wrote says which kind: an earlier render, or a hand edit.
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
            if text_digest(live_text) == stamp.get("instructionsDigest"):
                problems.append(
                    f"CLAUDE.md is the file the last install wrote, and this checkout and "
                    f"{local_path} now render a different one"
                )
            else:
                problems.append(
                    "CLAUDE.md was edited since the last install, or predates whole-file "
                    "ownership, so a re-run backs it up before rewriting it. Move host-specific "
                    f"content into {local_path}"
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
    contain_dst = hooks_dir / CONTAIN_NAME
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
    print(f"Installing agent host-safety kit into: {claude_home}")
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
        try:
            write_regular_file(
                claude_md,
                rendered.replace("\n", newline).encode("utf-8"),
                prior=raw,
                before=(lambda: backups.append(write_backup(claude_md, raw)))
                if needs_backup
                else None,
            )
        except IncompleteWrite as e:
            if backups:
                kept = f"Its prior content is backed up at {backups[0]}."
            elif raw is not None:
                kept = (
                    "Its prior content was the last install's render, so nothing written by hand "
                    "was lost, though a re-run backs up the partial file before rewriting it."
                )
            else:
                kept = "It did not exist before this run."
            sys.stderr.write(
                f"{e}. New sessions load that partial file until a re-run succeeds. {kept} "
                "Fix what stopped the write, then re-run. No hook or setting was changed.\n"
            )
            return 1
        except OSError as e:
            kept = f" Its prior content is backed up at {backups[0]}." if backups else ""
            sys.stderr.write(
                f"Nothing was installed: {claude_md} could not be rewritten ({e}).{kept} "
                "Fix what the error names, or move the file aside, then re-run.\n"
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
            sys.stderr.write(f"staging {src_name} failed ({e}); no hook was replaced.\n")
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
                f"{src_name} self-test FAILED; no hook was replaced.\n" + r.stdout + r.stderr
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
    try:
        settings_raw = read_regular_file(settings)
        if settings_raw is None:
            raise NotRegularFile(f"{settings} is not a regular file")
        settings_text = settings_raw.decode("utf-8")
    except FileNotFoundError:
        settings_raw, settings_text = None, ""
    except (ValueError, OSError) as e:
        sys.stderr.write(
            f"{settings} cannot be read ({e}). Fix or move it aside, then re-run. This file is "
            "unchanged, so the hook is deployed but not registered.\n"
        )
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
        ("env", dict),
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
            hooks_list[:] = [
                h for h in hooks_list if not names_hook(h.get("command", ""), GUARD_NAME)
            ]
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
            hooks_list[:] = [
                h for h in hooks_list if not names_hook(h.get("command", ""), SWEEP_NAME)
            ]
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
    if not env:
        del data["env"]

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
    try:
        write_regular_file(
            settings, (json.dumps(data, indent=2) + "\n").encode("utf-8"), prior=settings_raw
        )
    except IncompleteWrite as e:
        remedy = (
            "It did not exist before this run, so remove it"
            if settings_raw is None
            else "Put its prior content back from a copy, or remove it"
        )
        sys.stderr.write(
            f"{e}. Claude Code cannot parse it as it stands, and a re-run refuses it too. Fix what "
            f"stopped the write. {remedy}, then re-run.\n"
        )
        return 1
    except OSError as e:
        sys.stderr.write(
            f"{settings} could not be written ({e}). This file is unchanged, so the hook is "
            "deployed but not registered. Fix what the error names, then re-run.\n"
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
    print(f'  {launcher} "{install_path}" --report')
    print("Restart Claude Code sessions on this machine so the hook and CLAUDE.md load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
