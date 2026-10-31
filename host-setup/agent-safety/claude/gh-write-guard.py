#!/usr/bin/env python3
"""PreToolUse guard: deny the plain shapes of the write, checkout, and wait incidents.

Registered as a Claude Code PreToolUse hook on the Bash tool. It reads the tool-input JSON on
stdin and classifies the command.
It prints a deny decision with a reason when the command matches a requirement in host-setup/agent-safety/README.md. Everything else passes through.

It bounds a good-faith agent's mistake rather than an adversary, and it is not a sandbox. One
simple lexer reads the command, and a requirement denies only the shape it names where that shape
appears plainly. A shape the lexer does not read is allowed. The one exception is a push to a
protected-by-default branch whose live rules cannot be read, which is denied. The README
"Requirements" section is the spec this implements, and its "Why This Exists" section states the
threat model.

The denied shapes, numbered as the README numbers them:

  1. a GitHub write (a mutating gh call or a git push) whose output is discarded or forced to success:
     >/dev/null, 2>/dev/null, &>/dev/null, || true, || :, || echo
  2. a GraphQL mutation carrying a literal node id rather than a captured variable
  3. a gh write naming a target under another owner than the checkout origin's, unless the
     maintainer granted it in GH_WRITE_GUARD_ALLOW
  4. a git operation that lands only by bypassing a branch rule:
     - gh pr merge --admin
     - a commit or push skipping hooks
     - a push the branch's live rules forbid
  5. a hand-rolled review-thread reply or resolve, where scripts/pr_review.py reply --resolve is
     the one-call path
  6. a mutating git operation run directly in a primary checkout rather than a linked worktree,
     unless GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT grants it
  7. a while, until, or arithmetic for loop that sleeps with no bound

Run `gh-write-guard.py --selftest` to check the decision matrix without Claude Code.
"""

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from urllib.parse import quote

_GH_WRITE_SUBS: dict[str, tuple[str, ...]] = {
    "pr": (
        *("create", "comment", "close", "merge", "edit"),
        *("review", "reopen", "ready", "lock", "unlock"),
        *("revert", "update-branch"),
    ),
    "issue": (
        *("create", "comment", "close", "edit", "reopen", "delete"),
        *("lock", "unlock", "pin", "unpin", "transfer", "develop"),
    ),
    "release": ("create", "edit", "delete", "upload", "delete-asset"),
    "repo": ("create", "delete", "edit", "rename", "archive", "unarchive", "sync", "fork"),
    **{g: ("create", "delete", "edit", "set") for g in ("secret", "variable", "ruleset")},
    "label": ("create", "delete", "edit", "set", "clone"),
    "gist": ("create", "edit", "delete"),
    "workflow": ("run", "enable", "disable"),
    "run": ("rerun", "cancel", "delete"),
    "cache": ("delete",),
    "project": (
        *("close", "copy", "create", "delete", "edit", "link", "unlink", "mark-template"),
        *("field-create", "field-delete", "item-add", "item-archive", "item-create"),
        *("item-delete", "item-edit"),
    ),
}
_GH_WRITE_SUBSUBS: dict[tuple[str, str], tuple[str, ...]] = {
    ("repo", "deploy-key"): ("add", "delete"),
    ("repo", "autolink"): ("create", "delete"),
}
_GH_REPO_POSITIONAL = frozenset({"edit", "delete", "rename", "archive", "unarchive", "sync"})
# A long flag takes the next word as its value unless it is one of these.
# A value-taking flag gh adds later is then still read.
_GH_BOOL_FLAGS = frozenset(
    {
        *("--admin", "--auto", "--squash", "--merge", "--rebase", "--delete-branch", "--draft"),
        *("--fill", "--fill-first", "--fill-verbose", "--web", "--yes", "--approve", "--comment"),
        *("--request-changes", "--paginate", "--slurp", "--include", "--silent", "--verbose"),
        *("--edit-last", "--private", "--public", "--internal", "--clone", "--confirm", "--force"),
        *("--dry-run", "--latest", "--prerelease", "--generate-notes", "--disable-auto"),
    }
)
# A short flag means different things in different subcommands, so only these take a value.
_GH_SHORT_VALUES = frozenset(
    {"-R", "-t", "-b", "-F", "-l", "-L", "-B", "-H", "-S", "-q", "-o", "-e"}
)
_GH_API_SHORT_VALUES = frozenset({"-R", "-f", "-F", "-H", "-X", "-q", "-t", "-p"})
_GH_REPO_SHORT_VALUES = _GH_SHORT_VALUES | {"-d", "-h", "-s"}
_GH_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field"})

_PROTECTED_DEFAULT = ("main", "master", "develop")

_ALWAYS_DENY_SUBS = frozenset(
    {"reset", "rebase", "cherry-pick", "revert", "restore", "add", "commit", "rm", "mv", "apply"}
    | {"am", "push"}
)
_VERDICT_SUBS = (
    _ALWAYS_DENY_SUBS | {"checkout", "switch", "merge", "pull", "stash", "clean"} | {"worktree"}
)
# The checkout and switch flags that create, reset, or force, where git's own overwrite check does not apply.
_CHECKOUT_FORCE_FLAGS = frozenset(
    {"-b", "-B", "-c", "-C", "--force", "-f", "--discard-changes", "--orphan", "--create"}
    | {"--force-create"}
)
_CHECKOUT_FORCE_CHARS = frozenset("bBcCf")

_ALLOW_ENV = "GH_WRITE_GUARD_ALLOW"
_ALLOW_PRIMARY_ENV = "GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT"
_FALSY_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})

_NODE_ID_LITERAL = re.compile(r"^(?:[A-Z]{1,5}_[A-Za-z0-9_\-]{12,}|MD[A-Za-z0-9]{12,})$")
_NODE_ID_IN_QUERY = re.compile(r'"([A-Z]{1,5}_[A-Za-z0-9_\-]{12,}|MD[A-Za-z0-9]{12,})"')
_MUTATION = re.compile(r"\bmutation\b")
_RESOLVE_THREAD = re.compile(r"\bresolveReviewThread\b")
_REPLY_ENDPOINT = re.compile(r"\bpulls/\d+/comments/\d+/replies\b")
_REPOS_PATH = re.compile(r"^/?repos/([^/\s]+)/([^/\s?]+)")
_GH_URL = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/(?:pull|issues)/\d+")
_URL_OWNER_REPO = re.compile(r"[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$")

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_LEADERS = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time"}
    | {"esac"}
)
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_REDIR_OP = re.compile(r"<<<|>>|>&|<&|>\||<>|>|<")
_SEPARATORS = ("&&", "||", ";;", "|&", ";", "&", "|")
_XARGS_VALUE_OPTS = frozenset({"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"})
_ENV_LONG = {"argv0": "a", "chdir": "C", "split-string": "S", "unset": "u"} | {
    "ignore-environment": "i",
    **dict.fromkeys(("block-signal", "default-signal", "ignore-signal", "debug", "null"), ""),
    **dict.fromkeys(("list-signal-handling", "help", "version"), ""),
}
_ENV_VALUE_LETTERS = frozenset("aCPSu")
_HOME_PREFIX = re.compile(r"^(?:~|\$HOME|\$\{HOME\})(?=/|\\|$)")

_MSYS_DRIVE_PATHS = os.name == "nt"
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")

_UNRESOLVED = object()


@dataclass
class _Seg:
    """One command between separators, as its words, its redirections, and the separator after it."""

    words: list[str]
    redirs: list[tuple[str, str]]
    op: str


def _tokenize(text: str, i: int = 0, nested: bool = False) -> tuple[list[_Seg], int]:
    """Split shell text into segments, honoring quotes, backslash escapes, and heredocs.

    A `$(...)`, backtick, or `${...}` span stays inside its word, unread. A heredoc body is data
    and is dropped. In `nested` mode the scan ends after the `)` that closes a `$(` span, and the
    index after it is returned with the segments.
    """
    segs: list[_Seg] = []
    words: list[str] = []
    redirs: list[tuple[str, str]] = []
    cur: list[str] = []
    in_word = False
    redir: str | None = None
    pending: list[tuple[str, bool]] = []
    depth = 0
    n = len(text)

    def end_word() -> None:
        nonlocal in_word, redir
        if not in_word:
            return
        word = "".join(cur)
        cur.clear()
        in_word = False
        if redir is not None:
            redirs.append((redir, word))
            redir = None
        else:
            words.append(word)

    def end_seg(op: str) -> None:
        nonlocal words, redirs
        end_word()
        if words or redirs or op in ("(", ")"):
            segs.append(_Seg(words, redirs, op))
        words, redirs = [], []

    while i < n:
        c = text[i]
        if c == "\\":
            if text.startswith("\\\n", i):
                i += 2
                continue
            cur.append(text[i + 1 : i + 2])
            in_word = True
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            cur.append(text[i + 1 : j])
            in_word = True
            i = j + 1
        elif c == '"':
            i = _double_quoted(text, i + 1, cur)
            in_word = True
        elif c == "`" or text.startswith(("$(", "${"), i):
            j = _span_end(text, i)
            cur.append(text[i:j])
            in_word = True
            i = j
        elif c == "#" and not in_word:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_seg("\n")
            i = _skip_heredocs(text, i + 1, pending)
            pending.clear()
        elif c in "<>":
            fd = "".join(cur) if in_word and "".join(cur).isdigit() else ""
            if fd:
                cur.clear()
                in_word = False
            end_word()
            if text.startswith("<<", i) and not text.startswith("<<<", i):
                i = _heredoc_opener(text, i + 2, pending)
                continue
            m = _REDIR_OP.match(text, i)
            assert m is not None
            redir = fd + m.group()
            i = m.end()
        elif c == "(":
            if (
                (redir is not None and not in_word)
                or (in_word and _opens_group("".join(cur)))
                or (not in_word and text.startswith("((", i))
            ):
                j = _paren_end(text, i)
                cur.append(text[i:j])
                in_word = True
                i = j
            else:
                end_seg("(")
                depth += 1
                i += 1
        elif c == ")":
            if nested and depth == 0:
                end_seg("")
                return segs, i + 1
            end_seg(")")
            depth = max(depth - 1, 0)
            i += 1
        elif text.startswith("&>", i):
            end_word()
            redir = "&>>" if text.startswith("&>>", i) else "&>"
            i += len(redir)
        elif c in ";&|":
            op = next(o for o in _SEPARATORS if text.startswith(o, i))
            end_seg(op)
            i += len(op)
        else:
            cur.append(c)
            in_word = True
            i += 1
    end_seg("")
    return segs, n


def _opens_group(word: str) -> bool:
    """True when a `(` after `word` opens an array, an extglob group, or a process substitution."""
    return word[-1:] != "" and word[-1:] in "=@?*+!<>"


def _double_quoted(text: str, i: int, cur: list[str]) -> int:
    """Append a double-quoted span's text to `cur`, returning the index after its closing quote."""
    n = len(text)
    while i < n and text[i] != '"':
        if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\$`\n':
            if text[i + 1] != "\n":
                cur.append(text[i + 1])
            i += 2
        elif text[i] == "`" or text.startswith(("$(", "${"), i):
            j = _span_end(text, i)
            cur.append(text[i:j])
            i = j
        else:
            cur.append(text[i])
            i += 1
    return i + 1


def _span_end(text: str, i: int) -> int:
    """The index after the backtick, `$(...)`, or `${...}` span starting at `i`."""
    if text[i] == "`":
        j = i + 1
        while j < len(text) and text[j] != "`":
            j += 2 if text[j] == "\\" else 1
        return min(j + 1, len(text))
    if text.startswith("$(", i):
        return _paren_end(text, i + 1)
    j = text.find("}", i)
    return len(text) if j < 0 else j + 1


def _paren_end(text: str, i: int) -> int:
    """The index after the `)` matching the `(` at `i`."""
    return _tokenize(text, i + 1, nested=True)[1]


def _heredoc_opener(text: str, j: int, pending: list[tuple[str, bool]]) -> int:
    """Record the heredoc whose delimiter word starts at `j`, returning the index after it."""
    dash = text.startswith("-", j)
    j += dash
    while j < len(text) and text[j] in " \t":
        j += 1
    delim: list[str] = []
    while j < len(text) and text[j] not in " \t\n;&|<>()":
        if text[j] in "'\"":
            k = text.find(text[j], j + 1)
            k = len(text) if k < 0 else k
            delim.append(text[j + 1 : k])
            j = k + 1
        elif text[j] == "\\":
            delim.append(text[j + 1 : j + 2])
            j += 2
        else:
            delim.append(text[j])
            j += 1
    if delim:
        pending.append(("".join(delim), dash))
    return j


def _skip_heredocs(text: str, i: int, pending: list[tuple[str, bool]]) -> int:
    """The index past the bodies of the heredocs opened on the line that just ended."""
    for delim, dash in pending:
        while i < len(text):
            j = text.find("\n", i)
            line = text[i : len(text) if j < 0 else j]
            i = len(text) if j < 0 else j + 1
            if (line.lstrip("\t") if dash else line) == delim:
                break
    return i


def _parse(words: list[str]) -> tuple[dict[str, str], list[str]]:
    """A segment's leading assignments and its command words, past any reserved word before them."""
    i = 0
    while i < len(words) and words[i] in _LEADERS:
        i += 1
    assigns: dict[str, str] = {}
    while i < len(words) and (m := _ASSIGN.match(words[i])):
        assigns[m[1]] = m[2]
        i += 1
    return assigns, words[i:]


def _base(word: str) -> str:
    """A command word's program name, without its directory or a `.exe` suffix."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


@dataclass
class _Launch:
    """A command's words past its launchers, and how an `env` among them changes what it runs in.

    `env` holds the assignments, `unset` each name an `env -u` removes from the environment the
    command inherits, and `cleared` whether an `env -i` empties it. `chdirs` holds each directory an
    `env -C` moves the command to, in turn.
    """

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    unset: set[str] = field(default_factory=set)
    cleared: bool = False
    chdirs: list[str] = field(default_factory=list)
    timed: bool = False


def _env_option(word: str, nxt: str | None) -> tuple[list[tuple[str, str]], int] | None:
    """The `env` options a word sets, as (letter, value) pairs, and the words they use, or None for an operand.

    A short cluster such as `-iC dir` and an unambiguous long prefix such as `--ch=dir` read the way
    `env` reads them.
    """
    if word == "-":
        return [("i", "")], 1
    if word == "--":
        return [], 1
    if word.startswith("--"):
        name, eq, value = word[2:].partition("=")
        hits = [k for k in _ENV_LONG if k.startswith(name)]
        letter = _ENV_LONG.get(name, _ENV_LONG[hits[0]] if len(hits) == 1 else "")
        if letter and letter in _ENV_VALUE_LETTERS and not eq:
            return [(letter, nxt or "")], 2
        return [(letter, value)], 1
    if not word.startswith("-"):
        return None
    opts: list[tuple[str, str]] = []
    for j, ch in enumerate(word[1:], 2):
        if ch in _ENV_VALUE_LETTERS:
            return (opts + [(ch, word[j:])], 1) if word[j:] else (opts + [(ch, nxt or "")], 2)
        opts.append((ch, ""))
    return opts, 1


def _split_string(value: str) -> list[str]:
    """The words an `env -S` string splits into, or its whitespace-separated words where quoting fails."""
    try:
        return shlex.split(value, comments=True)
    except ValueError:
        return value.split()


def _launch(argv: list[str]) -> _Launch:
    """Strip the launchers before a command, reading what an `env` among them changes."""
    out = _Launch([])
    env = out.env
    while argv:
        name, rest = _base(argv[0]), argv[1:]
        i = 0
        if name == "timeout":
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] in ("-s", "-k", "--signal", "--kill-after") else 1
            if i >= len(rest):
                break
            argv, out.timed = rest[i + 1 :], True
        elif name in ("nohup", "command", "exec", "nice"):
            if name == "command" and rest[:1] in (["-v"], ["-V"]):
                out.argv = []
                return out
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] == "-n" else 1
            argv = rest[i:]
        elif name == "env":
            assigned = False
            while i < len(rest):
                nxt = rest[i + 1] if i + 1 < len(rest) else None
                got = None if assigned else _env_option(rest[i], nxt)
                if got is None:
                    if not (m := _ASSIGN.match(rest[i])):
                        break
                    env[m[1]], assigned = m[2], True
                    i += 1
                    continue
                opts, used = got
                i += used
                for letter, value in opts:
                    if letter == "i":
                        env.clear()
                        out.cleared = True
                    elif letter == "u":
                        env.pop(value, None)
                        out.unset.add(value)
                    elif letter == "C":
                        out.chdirs.append(value)
                    elif letter == "S":
                        rest = rest[:i] + _split_string(value) + rest[i:]
            argv = rest[i:]
        elif name == "xargs":
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] in _XARGS_VALUE_OPTS else 1
            argv = rest[i:]
        else:
            break
    out.argv = argv
    return out


def _wrapper_body(argv: list[str]) -> str | None:
    """The command string an `eval` or a `sh -c`-style shell runs, or None."""
    name = _base(argv[0])
    if name == "eval":
        return " ".join(argv[1:])
    if name not in _SHELLS:
        return None
    i = 1
    while i < len(argv) and argv[i].startswith(("-", "+")) and argv[i] != "--":
        if argv[i] in ("-o", "+o"):
            i += 2
            continue
        if not argv[i].startswith("--") and "c" in argv[i][1:]:
            return argv[i + 1] if i + 1 < len(argv) else None
        i += 1
    return None


@dataclass
class _Cmd:
    """One command the text runs, with the environment and directory it runs in.

    `own` holds the assignments the command itself carries, before it or through `env`.
    """

    argv: list[str]
    env: dict[str, str]
    cwd: str | None
    quiet: bool
    own: dict[str, str] = field(default_factory=dict)


@dataclass
class _Reading:
    """Every command read from the text, and each text's segments for the wait rule."""

    cmds: list[_Cmd] = field(default_factory=list)
    texts: list[tuple[list[_Seg], bool]] = field(default_factory=list)


def _read(
    text: str,
    cwd: str | None,
    env: dict[str, str],
    out: _Reading,
    depth: int = 0,
    quiet: bool = False,
    timed: bool = False,
) -> None:
    """Read each command in `text` into `out`, following `cd` and `export` through the text.

    A wrapper body is read once, at `depth` 1, with the directory and environment the wrapper sees.
    """
    segs, _ = _tokenize(text)
    out.texts.append((segs, timed))
    saved: list[tuple[str | None, dict[str, str]]] = []
    for k, seg in enumerate(segs):
        # A subshell's cd and export end at its closing parenthesis.
        if k and segs[k - 1].op == "(":
            saved.append((cwd, dict(env)))
        elif k and segs[k - 1].op == ")" and saved:
            cwd, env = saved.pop()
        assigns, argv = _parse(seg.words)
        launch = _launch(argv)
        argv = launch.argv
        if not argv:
            env.update(assigns)
            continue
        kept = {} if launch.cleared else {**env, **assigns}
        kept = {k: v for k, v in kept.items() if k not in launch.unset}
        here = {**kept, **launch.env}
        own = {**{k: v for k, v in assigns.items() if k in kept}, **launch.env}
        where = _moved(cwd, launch.chdirs)
        name = _base(argv[0])
        silenced = quiet or _discards(seg.redirs) or _forced(segs, k)
        if name == "export":
            env.update((m[1], m[2]) for a in argv[1:] if (m := _ASSIGN.match(a)))
        elif name in ("cd", "pushd"):
            cwd = _cd(cwd, argv[1:])
        elif name == "popd":
            cwd = None
        elif (body := _wrapper_body(argv)) is not None:
            if depth == 0:
                bounded = launch.timed and name in _SHELLS
                _read(body, where, dict(here), out, 1, silenced, bounded)
        else:
            out.cmds.append(_Cmd(argv, here, where, silenced, own))


def _discards(redirs: list[tuple[str, str]]) -> bool:
    """True when a redirection sends output to /dev/null."""
    return any(">" in op and target == "/dev/null" for op, target in redirs)


def _forced(segs: list[_Seg], k: int) -> bool:
    """True when segment `k` is followed by `|| true`, `|| :`, or `|| echo`."""
    if segs[k].op != "||" or k + 1 >= len(segs):
        return False
    words = segs[k + 1].words
    return bool(words) and _base(words[0]) in ("true", ":", "echo")


def _home() -> str:
    return os.path.expanduser("~")


def _msys_drive_path(value: str | None) -> str | None:
    """Rewrite a Git Bash drive spelling such as `/c/repos/x` as `C:/repos/x`, on Windows only."""
    if not _MSYS_DRIVE_PATHS or not value:
        return value
    m = _MSYS_DRIVE_RE.match(value)
    return f"{m.group(1).upper()}:{value[m.end() :] or '/'}" if m else value


def _join_dir(base: str | None, value: str) -> str | None:
    """The directory `value` names from `base`, or None where the text does not name one.

    A run-time word, a directory-stack word, and on Windows a rooted path with no drive name none.
    """
    if not value or re.fullmatch(r"[+-]\d*", value) or value.startswith(("~+", "~-")):
        return None
    value = _HOME_PREFIX.sub(lambda _m: _home(), value)
    if "$" in value or "`" in value:
        return None
    value = _msys_drive_path(value) or value
    if value[:1] in "/\\":
        return None if _MSYS_DRIVE_PATHS else value
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return value
    if base is None:
        return None
    return base.rstrip("/\\") + "/" + value


def _moved(where: str | None, steps: list[str]) -> str | None:
    """The directory each step moves `where` to in turn, the way git composes its `-C` values."""
    for step in steps:
        where = _join_dir(where, step)
    return where


def _cd(cwd: str | None, args: list[str]) -> str | None:
    """The directory a `cd` or `pushd` with these arguments moves to."""
    args = [a for a in args if a not in ("-P", "-L", "-e", "-@", "--")]
    return _join_dir(cwd, args[0]) if args else _home()


@dataclass
class _Git:
    """A git command's global options, subcommand, and the arguments after it."""

    c_dirs: list[str]
    configs: list[str]
    git_dir: str | None
    sub: str | None
    args: list[str]


def _git(argv: list[str]) -> _Git:
    """Read a git argv's global options before its subcommand."""
    g = _Git([], [], None, None, [])
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "-c", "--config-env", "--git-dir", "--work-tree", "--namespace"):
            value = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        elif a.startswith(("-C", "-c")) and len(a) > 2:
            a, value = a[:2], a[2:]
            i += 1
        elif a.startswith(("--config-env=", "--git-dir=")):
            a, value = a.split("=", 1)
            i += 1
        elif a.startswith("-"):
            i += 1
            continue
        else:
            g.sub, g.args = a, argv[i + 1 :]
            return g
        if a == "-C":
            g.c_dirs.append(value)
        elif a in ("-c", "--config-env"):
            g.configs.append(value)
        elif a == "--git-dir":
            g.git_dir = value
    return g


def _git_stdout(cwd: str | None, args: list[str]) -> str | None:
    """`git -C cwd <args>`'s stripped output, or None when it fails or cannot run."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd or ".", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=5,
            check=False,
        )
    # The hook answers on any failure rather than crash.
    except Exception:  # noqa: BLE001
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _origin_owner_repo(cwd: str | None) -> tuple[str, str] | None:
    """The (owner, repo) of the checkout's origin, lowercased, or None."""
    url = _git_stdout(cwd, ["remote", "get-url", "origin"])
    m = _URL_OWNER_REPO.search(url or "")
    return (m.group(1).lower(), m.group(2).lower()) if m else None


def _is_primary_checkout(target_dir: str) -> bool | None:
    """True for a primary checkout, False for a linked worktree, None where no repository resolves.

    The test compares `--git-dir` with `--git-common-dir`, since a submodule's `.git` is a file
    in a primary working tree. `--path-format=absolute` must come before the two paths.
    """
    out = _git_stdout(
        target_dir, ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]
    )
    lines = (out or "").splitlines()
    return lines[0].strip() == lines[1].strip() if len(lines) == 2 else None


def _resolves_as_ref(target_dir: str, ref: str) -> bool:
    """Whether `ref` names a commit in `target_dir`, the test git uses to read a bare checkout argument."""
    return (
        _git_stdout(target_dir, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"]) is not None
    )


def _current_push_branch(where: str | None, git_dir: str | None = None) -> str | None:
    """The branch checked out in `where`, or in `git_dir`, or None when HEAD is detached or unreadable."""
    query = ["--git-dir", git_dir] if git_dir else []
    name = _git_stdout(where, [*query, "rev-parse", "--abbrev-ref", "HEAD"])
    return name if name and name != "HEAD" else None


def _bare_push_branch(
    cwd: str | None,
    c_dirs: list[str],
    git_dir: str | None,
    leading_cd: str | None,
    moved: bool,
) -> object:
    """The branch a push naming no refspec writes, or `_UNRESOLVED` where the text does not name its checkout.

    `leading_cd` and then each `-C` value in `c_dirs` move the push from `cwd`, and a `git_dir`
    names the repository from there. A push `moved` to a directory the text does not name is
    unresolved unless its `git_dir` is absolute. So is one whose `git_dir` the reading does not
    resolve.
    """
    where = None
    if not moved:
        where = _moved(_msys_drive_path(cwd), [leading_cd] if leading_cd else [])
        where = _moved(where, c_dirs)
    if git_dir is not None:
        named = _join_dir(where, git_dir)
        return _UNRESOLVED if named is None else _current_push_branch(where, named)
    return _UNRESOLVED if where is None else _current_push_branch(where)


def _live_branch_rules(owner: str, repo: str, branch: str) -> set[str] | None:
    """The active rule types on a branch, or None when the query cannot be answered."""
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/rules/branches/{quote(branch, safe='')}"]
            + ["--jq", "[.[].type]"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        return set(json.loads(r.stdout or "[]")) if r.returncode == 0 else None
    # Any failure means unknown, so a protected-by-default branch fails closed.
    except Exception:  # noqa: BLE001
        return None


def _branch_name(ref: str) -> str | None:
    """The branch a push destination names, or None for a tag, a pattern, or a run-time word."""
    if not ref or any(ch in ref for ch in "$`*"):
        return None
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return None if ref.startswith("refs/") else ref


def _push_targets(
    args: list[str], branch_of: Callable[[], str | None]
) -> tuple[str | None, list[tuple[str, str]]]:
    """A push's remote and each (operation, branch) it writes, the operation update, force,
    delete, or mirror."""
    force = delete = every = mirror = tags = False
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1 :]
            break
        if not a.startswith("-") or a == "-":
            pos.append(a)
        elif a in ("--force", "--force-if-includes") or a.startswith("--force-with-lease"):
            force = True
        elif a == "--delete":
            delete = True
        elif a in ("--all", "--branches"):
            every = True
        elif a == "--mirror":
            mirror = True
        elif a == "--tags":
            tags = True
        elif a in ("--repo", "--receive-pack", "--exec", "-o", "--push-option"):
            i += 1
        elif re.fullmatch(r"-[A-Za-z]+", a):
            force, delete = force or "f" in a, delete or "d" in a
            i += a.endswith("o")
        i += 1
    remote, specs = (pos[0], pos[1:]) if pos else (None, [])
    write = "force" if force else "update"
    if mirror:
        return remote, [("mirror", b) for b in _PROTECTED_DEFAULT]
    if every:
        return remote, [(write, b) for b in _PROTECTED_DEFAULT]
    if not specs:
        branch = None if delete or tags else branch_of()
        return remote, [(write, branch)] if branch else []
    out: list[tuple[str, str]] = []
    for spec in specs:
        op = "force" if force or spec.startswith("+") else "update"
        spec = spec.lstrip("+")
        if delete:
            op, dst = "delete", spec
        elif ":" in spec:
            src, dst = spec.split(":", 1)
            op = op if src else "delete"
        else:
            dst = spec
        name = branch_of() if dst in ("HEAD", "@") else _branch_name(dst)
        if name:
            out.append((op, name))
    return remote, out


def _named_git_dir(cmd: _Cmd, git: _Git) -> str | None:
    """The repository a `--git-dir`, or else a `GIT_DIR` the command carries, names, as written.

    A `GIT_DIR` set earlier in the text reads as a run-time word. Whether git sees it turns on an
    export or an `unset` the reading does not track.
    """
    if git.git_dir is not None:
        return git.git_dir
    if "GIT_DIR" in cmd.own:
        return cmd.own["GIT_DIR"]
    return "$GIT_DIR" if "GIT_DIR" in cmd.env else None


def _overrides_remote(cmd: _Cmd, git: _Git) -> bool:
    """Whether the text sets config that can change a remote's URL, which the reading does not apply."""
    keys = [c.split("=", 1)[0].lower() for c in git.configs]
    return any(k.startswith(("remote.", "url.")) for k in keys) or any(
        name.startswith("GIT_CONFIG") for name in cmd.env
    )


def _push_repo(cmd: _Cmd, git: _Git, remote: str | None) -> tuple[str, str] | None:
    """The (owner, repo) a push writes, read from its URL or its remote's push URL.

    The remote is read in the repository git uses, the one a `--git-dir` or `GIT_DIR` names over
    the directory's own. A config override of a remote, or a repository the text does not name,
    resolves to None.
    """
    remote = remote or "origin"
    url: str | None = remote
    if not ("://" in remote or re.match(r"^[^/\s]+@[^/\s]+:", remote)):
        where = _moved(cmd.cwd, git.c_dirs)
        named = _named_git_dir(cmd, git)
        git_dir = None if named is None else _join_dir(where, named)
        located = where is not None if named is None else git_dir is not None
        query = ["--git-dir", git_dir] if git_dir else []
        query += ["remote", "get-url", "--push", remote]
        unread = _overrides_remote(cmd, git) or not located
        url = None if unread else _git_stdout(where, query)
    m = _URL_OWNER_REPO.search(url or "")
    return (m.group(1).lower(), m.group(2).lower()) if m else None


def _rule_denial(op: str, br: str, rules: set[str] | None) -> str | None:
    """Why a push operation on `br` bypasses `rules`, or None. Unknown rules deny a protected default."""
    if rules is None:
        if br not in _PROTECTED_DEFAULT:
            return None
        return (
            f"Could not read the live rules of '{br}', a protected-by-default branch "
            "(main/master/develop), because the API did not answer or the repository the push "
            "writes could not be determined. Failing closed rather than risk a silent bypass "
            "under the maintainer's admin identity. Push to a feature branch and open a pull "
            "request."
        )
    if op == "mirror" and rules & {
        "non_fast_forward",
        "required_linear_history",
        "deletion",
        "pull_request",
    }:
        return f"This mirror push force-updates and deletes '{br}', whose rules forbid it."
    if op == "force" and rules & {"non_fast_forward", "required_linear_history"}:
        return (
            f"This force-pushes '{br}', whose rules forbid rewriting history. Never force-push a "
            "protected branch, land changes as follow-up commits."
        )
    if op == "delete" and "deletion" in rules:
        return f"This deletes '{br}', whose rules forbid deletion."
    if "pull_request" in rules:
        return (
            f"This push writes '{br}', whose rules require a pull request, so it lands only by "
            "bypassing that rule with admin power. Commit on a feature branch and open a pull "
            "request."
        )
    return None


def _skips_hooks(sub: str, args: list[str]) -> bool:
    """True when a commit or push carries `--no-verify`, or a commit its `-n` in a short cluster."""
    i = 0
    while i < len(args) and args[i] != "--":
        a = args[i]
        if a == "--no-verify":
            return True
        if sub == "commit" and re.fullmatch(r"-[A-Za-z]+", a):
            for ch in a[1:]:
                if ch == "n":
                    return True
                if ch in "mFcCt":
                    break
            i += a[-1] in "mFcCt"
        i += 1
    return False


def _bypass_denial(
    cmd: _Cmd, current_branch: str | None, rules_lookup: Callable[[str], set[str] | None] | None
) -> str | None:
    """Requirement 4: why this command lands only by bypassing a branch rule, or None."""
    name = _base(cmd.argv[0])
    if name == "gh":
        g = _gh(cmd.argv)
        if g.path[:2] == ["pr", "merge"] and any(f == "--admin" for f, _ in g.flags):
            return (
                "This merges with --admin, which overrides the branch's required reviews and "
                "checks. Merge without it once the pull request meets its gate."
            )
        return None
    if name != "git":
        return None
    git = _git(cmd.argv)
    if git.sub not in ("commit", "push"):
        return None
    if _skips_hooks(git.sub, git.args):
        return (
            "This uses --no-verify, which skips the git hooks (signing, lint, and pre-push "
            "gates). Skipping verification is a bypass, run the command without it."
        )
    for cfg in git.configs:
        key = cfg.split("=", 1)[0].lower()
        if key == "core.hookspath":
            return (
                "This overrides core.hooksPath for a commit or push, which runs whatever hooks "
                "that directory holds instead of the repository's. That is a hook bypass, run "
                "the command without the override."
            )
        if key == "include.path" or key.startswith("includeif."):
            return (
                "This pulls a config file into a commit or push by an include override, and that "
                "file may set core.hooksPath. Run the command without the override."
            )
    if git.sub != "push":
        return None

    def branch_of() -> str | None:
        if current_branch is not None:
            return current_branch
        named = _named_git_dir(cmd, git)
        got = _bare_push_branch(cmd.cwd, git.c_dirs, named, None, cmd.cwd is None)
        return got if isinstance(got, str) else None

    remote, targets = _push_targets(git.args, branch_of)
    repo: tuple[str, str] | None | object = _UNRESOLVED
    for op, br in targets:
        if rules_lookup is not None:
            rules = rules_lookup(br)
        else:
            if repo is _UNRESOLVED:
                repo = _push_repo(cmd, git, remote)
            rules = _live_branch_rules(repo[0], repo[1], br) if isinstance(repo, tuple) else None
        reason = _rule_denial(op, br, rules)
        if reason:
            return reason
    return None


def _primary_checkout_verdict(
    sub: str, args: list[str], target: str, ref_resolver: Callable[[str, str], bool]
) -> bool:
    """Whether requirement 6 denies this subcommand in a primary checkout."""
    if sub == "worktree":
        before = args[1 : args.index("--")] if "--" in args else args[1:]
        return args[:1] == ["remove"] and any(
            a in ("-f", "--force") or re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", a) for a in before
        )
    if sub in ("checkout", "switch"):
        if "--" in args or any(
            a in _CHECKOUT_FORCE_FLAGS
            or (
                a.startswith("-")
                and not a.startswith("--")
                and any(ch in _CHECKOUT_FORCE_CHARS for ch in a[1:])
            )
            for a in args
        ):
            return True
        positional = [a for a in args if a == "-" or not a.startswith("-")]
        if len(positional) != 1:
            return True
        return positional[0] != "-" and not ref_resolver(target, positional[0])
    if sub in ("merge", "pull"):
        return "--ff-only" not in args
    if sub == "stash":
        return args[:1] not in (["list"], ["show"])
    if sub == "clean":
        before = args[: args.index("--")] if "--" in args else args
        if any(a == "--dry-run" or re.fullmatch(r"-[A-Za-z]*n[A-Za-z]*", a) for a in before):
            return False
        return any(a == "--force" or re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", a) for a in before)
    return sub in _ALWAYS_DENY_SUBS


def _delete_only_push(args: list[str]) -> bool:
    """Whether a push only deletes remote branches outside the protected defaults.

    Such a push names its remote by name, so it moves no local branch and touches no working tree.
    Requirement 6's hazard is then absent, and requirement 4 still judges each branch's live rules.
    Any other option keeps the denial. So do a path or URL remote, a forced refspec, one with a
    source, a bare `:`, a `refs/tags/` name, and a run-time word. This reads apart from
    `_push_targets`, which drops a refspec it cannot read, since an exemption must deny one.
    """
    delete = False
    pos: list[str] = []
    for i, a in enumerate(args):
        if a == "--":
            pos += args[i + 1 :]
            break
        if not a.startswith("-"):
            pos.append(a)
        elif a in ("--delete", "--dry-run", "--quiet", "--verbose") or re.fullmatch(r"-[dnqv]+", a):
            delete = delete or a == "--delete" or (a[1] != "-" and "d" in a)
        else:
            return False
    if len(pos) < 2 or not re.fullmatch(r"\w[\w-]*", pos[0]):
        return False
    for spec in pos[1:]:
        dst = spec if delete else spec[1:] if spec.startswith(":") else ""
        name = _branch_name(dst) if ":" not in dst and not dst.startswith("+") else None
        if not name or name in _PROTECTED_DEFAULT or name in ("HEAD", "@"):
            return False
    return True


def _primary_denial(
    cmd: _Cmd,
    environ: dict[str, str],
    lookup: Callable[[str], bool | None],
    ref_resolver: Callable[[str, str], bool],
) -> str | None:
    """Requirement 6: why this is a mutating git operation in a primary checkout, or None."""
    if _base(cmd.argv[0]) != "git":
        return None
    git = _git(cmd.argv)
    if git.sub not in _VERDICT_SUBS:
        return None
    if git.sub == "push" and not git.configs and _delete_only_push(git.args):
        return None
    target = _moved(cmd.cwd, git.c_dirs)
    if target is None or not _primary_checkout_verdict(git.sub, git.args, target, ref_resolver):
        return None
    if environ.get(_ALLOW_PRIMARY_ENV, "").strip().lower() not in _FALSY_ENV_VALUES:
        return None
    if not lookup(target):
        return None
    return (
        f"This runs `git {git.sub}` directly in a primary checkout ({target}), not a linked "
        "worktree, where it can destroy another task's uncommitted work. Create a worktree for "
        "the task (git worktree add) and run it there. The maintainer grants an exception only "
        f"through {_ALLOW_PRIMARY_ENV}, set before the session starts. See GOVERNANCE.md "
        "'Repository Boundaries and Write Safety' and the repo-worktree skill."
    )


@dataclass
class _Gh:
    """A gh command's path words, positional arguments, and flags with their values."""

    path: list[str]
    positionals: list[str]
    flags: list[tuple[str, str | None]]


def _gh(argv: list[str]) -> _Gh:
    """Read a gh argv's path words, positionals, and flags."""
    g = _Gh([], [], [])
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            g.positionals += args[i + 1 :]
            break
        if a.startswith("-") and len(a) > 1:
            name, eq, value = a.partition("=")
            if eq:
                g.flags.append((name, value))
            elif a[:2] in ("-R", "-X") and len(a) > 2:
                g.flags.append((a[:2], a[2:]))
            elif i + 1 < len(args) and _takes_value(a, g.path, args[i + 1]):
                g.flags.append((a, args[i + 1]))
                i += 1
            else:
                g.flags.append((a, None))
        elif len(g.path) < (
            1 if g.path[:1] == ["api"] else 3 if tuple(g.path[:2]) in _GH_WRITE_SUBSUBS else 2
        ):
            g.path.append(a)
        else:
            g.positionals.append(a)
        i += 1
    return g


def _takes_value(flag: str, path: list[str], nxt: str) -> bool:
    """Whether a gh flag with no `=` takes the next word as its value, under its path words."""
    if path[1:2] in (["close"], ["reopen"]) and flag in ("--comment", "-c"):
        return not nxt.startswith("-")
    if flag.startswith("--"):
        return flag not in _GH_BOOL_FLAGS and (nxt == "-" or not nxt.startswith("-"))
    if path[:1] == ["api"]:
        return flag in _GH_API_SHORT_VALUES
    return flag in (_GH_REPO_SHORT_VALUES if path[:1] == ["repo"] else _GH_SHORT_VALUES)


def _gh_field(g: _Gh, key: str) -> str | None:
    """The value of the `-f`/`-F` field named `key`."""
    for flag, value in g.flags:
        if flag in _GH_FIELD_FLAGS and value and value.startswith(key + "="):
            return value[len(key) + 1 :]
    return None


def _api_method(g: _Gh) -> str:
    for flag, value in g.flags:
        if flag in ("-X", "--method") and value:
            return value.upper()
    sends = any(flag in _GH_FIELD_FLAGS or flag == "--input" for flag, _ in g.flags)
    return "POST" if sends else "GET"


def _gh_is_write(g: _Gh) -> bool:
    """True when a gh command is a known-mutating subcommand, a mutation, or a non-GET API call."""
    if g.path[:1] == ["api"]:
        if g.positionals[:1] in (["graphql"], ["/graphql"]):
            return bool(_MUTATION.search(_gh_field(g, "query") or ""))
        return _api_method(g) != "GET"
    if len(g.path) >= 2 and g.path[1] in _GH_WRITE_SUBS.get(g.path[0], ()):
        return True
    return len(g.path) >= 3 and g.path[2] in _GH_WRITE_SUBSUBS.get((g.path[0], g.path[1]), ())


def _literal_node_id(g: _Gh) -> str | None:
    """A literal node id a GraphQL mutation passes as a field or in its query text."""
    if g.path[:1] != ["api"] or g.positionals[:1] not in (["graphql"], ["/graphql"]):
        return None
    for flag, value in g.flags:
        if flag not in _GH_FIELD_FLAGS or not value or "=" not in value:
            continue
        name, literal = value.split("=", 1)
        if name == "query":
            if m := _NODE_ID_IN_QUERY.search(literal):
                return m.group(1)
        elif _NODE_ID_LITERAL.match(literal):
            return literal
    return None


def _repo_value(value: str) -> tuple[str, str] | None:
    """The (owner, repo) a `[HOST/]OWNER/REPO` or URL value names, or None for a run-time word."""
    if "$" in value or "`" in value:
        return None
    parts = [p for p in value.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[-2].lower(), parts[-1].removesuffix(".git").lower()


def _gh_targets(g: _Gh, env: dict[str, str]) -> list[tuple[str, str | None]]:
    """Each (owner, repo) a gh write names, the repo None where it names a whole owner."""
    out: list[tuple[str, str | None]] = []
    repos = [v for f, v in g.flags if f in ("-R", "--repo") and v]
    if g.path[:1] == ["api"]:
        m = _REPOS_PATH.match(g.positionals[0] if g.positionals else "")
        if m and not m.group(1).startswith(("{", "$")):
            out.append((m.group(1).lower(), m.group(2).lower()))
    elif not repos and env.get("GH_REPO"):
        repos.append(env["GH_REPO"])
    if g.path[:1] == ["repo"] and g.path[1:2] and g.path[1] in _GH_REPO_POSITIONAL:
        repos += g.positionals[:1]
    if g.path[:2] == ["issue", "transfer"]:
        repos += g.positionals[1:2]
    out += [t for r in repos if (t := _repo_value(r))]
    out += [
        (m.group(1).lower(), m.group(2).lower()) for p in g.positionals if (m := _GH_URL.match(p))
    ]
    out += [
        (v.lower(), None)
        for f, v in g.flags
        if f in ("--org", "--owner", "-o") and v and v != "@me" and not v.startswith("$")
    ]
    return out


def _granted_targets(environ: dict[str, str]) -> set[tuple[str, str]]:
    """Maintainer-granted targets, {(owner, repo)}, with repo '*' for a whole owner.

    Read from the environment the session was launched with, which an agent cannot set for itself.
    That is because an inline prefix or an `export` in a Bash call never reaches the hook's own process.
    """
    out = set()
    for tok in re.split(r"[,\s]+", environ.get(_ALLOW_ENV, "")):
        owner, _, repo = tok.partition("/")
        if owner.strip() and repo.strip():
            out.add((owner.strip().lower(), repo.strip().lower()))
    return out


def _permitted(
    target: tuple[str, str | None], origin: tuple[str, str], granted: set[tuple[str, str]]
) -> bool:
    """True when a write to `target` stays under the origin's owner or the maintainer granted it."""
    owner, repo = target
    return (
        owner == origin[0]
        or (owner, "*") in granted
        or (repo is not None and (owner, repo) in granted)
    )


def _hand_rolled(g: _Gh, granted: set[tuple[str, str]]) -> str | None:
    """Requirement 5: why this is a hand-rolled review-thread reply or resolve, or None."""
    helper = (
        'Use `scripts/pr_review.py reply <N> --repo <owner>/<repo> --match "<words from the '
        'finding>" --body "<answer>" --resolve` instead, which captures the thread id from a '
        "live query and posts the reply and the resolve as one call."
    )
    endpoint = g.positionals[0] if g.positionals and g.path[:1] == ["api"] else ""
    if endpoint.lstrip("/") == "graphql":
        query = _gh_field(g, "query") or ""
        if _MUTATION.search(query) and _RESOLVE_THREAD.search(query) and not granted:
            return "This resolves a review thread through `gh api graphql` by hand. " + helper
    elif _REPLY_ENDPOINT.search(endpoint) and _api_method(g) == "POST":
        m = _REPOS_PATH.match(endpoint)
        target = (m.group(1).lower(), m.group(2).lower()) if m else None
        if (target and (target in granted or (target[0], "*") in granted)) or (
            not target and granted
        ):
            return None
        return "This posts a review-comment reply to the REST replies endpoint by hand. " + helper
    return None


def _guarded(cond: list[str]) -> bool:
    """True when a loop condition carries a bound: an arithmetic guard or a `read` of its input."""
    cond = _parse(cond)[1]
    if cond[:1] == ["read"]:
        return True
    return any(
        w in ("-lt", "-le", "-gt", "-ge") or (w.startswith("((") and re.search(r"[<>]", w))
        for w in cond
    )


def _unbounded_wait(segs: list[_Seg], timed: bool) -> bool:
    """True when the text holds a loop that sleeps with no bound.

    `timed` means a `timeout` runs the shell reading this text, which bounds it unless the text
    forks work out of the timeout's reach.
    """
    forks = any(s.op == "&" or _parse(s.words)[1][:1] == ["setsid"] for s in segs)
    if timed and not forks:
        return False
    loops: list[list[bool]] = []
    for seg in segs:
        words = seg.words
        i = 0
        while i < len(words):
            w = words[i]
            if w in ("while", "until"):
                loops.append([_guarded(words[i + 1 :]), False])
            elif w == "for":
                arith = words[i + 1 : i + 2] and words[i + 1].startswith("((")
                loops.append([not arith or bool(re.search(r"[<>]", words[i + 1])), False])
                break
            elif w == "done":
                if loops and (loop := loops.pop())[1] and not loop[0]:
                    return True
                break
            elif w not in ("do", "then", "else", "elif", "if", "!", "{", "time"):
                if _base(w) == "sleep":
                    for loop in loops:
                        loop[1] = True
                break
            i += 1
    return any(sleeps and not guarded for guarded, sleeps in loops)


_WAIT_REASON = (
    "This is a shell wait with no bound: a while, until, or for ((;;)) loop that sleeps. A shell "
    "started by a tool call outlives the agent that started it, so an unbounded wait runs until "
    "the machine is rebooted. Bound it: run the loop under timeout, as in timeout 600 bash -c "
    "'<the loop>', or guard its condition with a counter, as in [ \"$i\" -lt 120 ]. See AGENTS.md "
    "'Delegation'."
)


def classify(
    cmd: str,
    cwd: str | None = None,
    origin: tuple[str, str] | None = None,
    current_branch: str | None = None,
    rules_lookup: Callable[[str], set[str] | None] | None = None,
    environ: dict[str, str] | None = None,
    primary_checkout_lookup: Callable[[str], bool | None] | None = None,
    ref_resolver: Callable[[str, str], bool] | None = None,
) -> tuple[str, str]:
    """Return (decision, reason), the decision 'allow' or 'deny'.

    Every argument after `cwd` is a seam the self-test fills for a deterministic offline run.
    The seams stand in for:
    - `origin`: the checkout origin's (owner, repo)
    - `current_branch`: the branch a push naming no refspec writes
    - `rules_lookup(branch)`: the live branch-rules query
    - `environ`: the environment the session was launched with
    - `primary_checkout_lookup(dir)`: the primary-checkout test
    - `ref_resolver(dir, ref)`: the live ref check
    """
    env_in: dict[str, str] = dict(os.environ) if environ is None else environ
    lookup = primary_checkout_lookup or _is_primary_checkout
    resolver = ref_resolver or _resolves_as_ref
    cwd = _msys_drive_path(cwd)
    reading = _Reading()
    _read(cmd, cwd, {"GH_REPO": env_in["GH_REPO"]} if env_in.get("GH_REPO") else {}, reading)
    for c in reading.cmds:
        if reason := _bypass_denial(c, current_branch, rules_lookup):
            return "deny", reason
    for c in reading.cmds:
        if reason := _primary_denial(c, env_in, lookup, resolver):
            return "deny", reason
    if any(_unbounded_wait(segs, timed) for segs, timed in reading.texts):
        return "deny", _WAIT_REASON
    granted = _granted_targets(env_in)
    for c in reading.cmds:
        name = _base(c.argv[0])
        is_push = name == "git" and _git(c.argv).sub == "push"
        g = _gh(c.argv) if name == "gh" else None
        if not is_push and not (g and _gh_is_write(g)):
            continue
        if c.quiet:
            return "deny", (
                "This is a GitHub write with its output discarded or forced to success "
                "(>/dev/null, 2>/dev/null, &>/dev/null, || true, || :, || echo). A write can "
                "succeed on the server while the client reports an error, so its result is "
                "exactly what must be read. Run it without that tail and read the response. See "
                "GOVERNANCE.md 'Repository Boundaries and Write Safety'."
            )
        if g is None:
            continue
        if node_id := _literal_node_id(g):
            return "deny", (
                f"This mutation passes a literal GitHub node id ({node_id[:16]}...) instead of a "
                "variable captured from a live query. Node ids resolve globally, so a fabricated "
                "or stale id writes to a real object in another repository. Capture the id from a "
                'query in this session into a variable and pass -F ...="$VAR". See GOVERNANCE.md '
                "'Repository Boundaries and Write Safety'."
            )
        if origin is None:
            origin = _origin_owner_repo(cwd)
        for t in _gh_targets(g, c.env):
            if origin and not _permitted(t, origin, granted):
                named = f"{t[0]}/{t[1]}" if t[1] else t[0]
                return "deny", (
                    f"This write targets {named}, under a different owner than this checkout's "
                    f"origin ({origin[0]}/{origin[1]}). Writes reach the origin and its sibling "
                    f"repositories under {origin[0]}, and a different owner is the shape that "
                    "caused a stray comment on a stranger's repository. Ask the maintainer to "
                    f'grant it in {_ALLOW_ENV} ("{t[0]}/<repo>", or "{t[0]}/*" for that whole '
                    "owner) before the session starts, and do not set it yourself. See "
                    "GOVERNANCE.md 'Repository Boundaries and Write Safety'."
                )
        if reason := _hand_rolled(g, granted):
            return "deny", reason
    return "allow", ""


_ORIGIN = ("example", "repo")
_PRIMARY = "/c/work/primary"
_WORKTREE = "/c/work/wt"
_REFS = frozenset({"develop", "main", "feature/x", "origin/develop"})
_STRANGER_REPLY = (
    "gh api graphql -f query='mutation($t:ID!){addPullRequestReviewThreadReply(input:"
    '{pullRequestReviewThreadId:$t,body:"x"}){comment{id}}}\''
)
_RESOLVE = (
    "gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t})"
    '{thread{isResolved}}}\' -F t="$TID"'
)
_ID_IN_QUERY = (
    "gh api graphql -f query='mutation{addComment(input:"
    '{subjectId:"I_kwDOExample12345",body:"x"}){clientMutationId}}\''
)
_COMMIT_THEN_PUSH = (
    "git commit -m \"$(cat <<'EOF'\nFix the thing\n\nIt's done (finally).\nEOF\n)\" "
    "&& git push origin develop"
)

# (command, want, source), run in the worktree with no rules on any branch.
# A source names the requirement whose plain shape a case is, the incident behind it, or the issue recording a shape a session hit.
_CASES: list[tuple[str, str, str]] = [
    ("gh pr comment 5 --body ok >/dev/null", "deny", "R1"),
    ("gh pr comment 5 --body ok 2>/dev/null", "deny", "R1"),
    ("gh pr comment 5 --body ok &>/dev/null", "deny", "R1"),
    ("gh issue close 5 || true", "deny", "R1"),
    ("gh issue close 5 || :", "deny", "R1"),
    ("gh issue close 5 || echo failed", "deny", "R1"),
    ("gh pr create --title x --body y >/dev/null 2>&1", "deny", "R1"),
    ("git commit -m x && git push origin feature/x >/dev/null 2>&1", "deny", "R1"),
    ("gh api repos/example/repo/issues/1/comments -f body=x 2>/dev/null", "deny", "R1"),
    ("bash -c 'gh pr comment 5 --body ok' >/dev/null", "deny", "R1"),
    ("gh pr view 5 --json state >/dev/null", "allow", "R1"),
    ("gh pr comment 5 --body ok 2>&1 | tee out.log", "allow", "R1"),
    ("gh pr comment 5 --body ok 2>&1", "allow", "R1"),
    ("return 1 2>/dev/null || exit 1", "allow", "R1"),
    ("gh pr comment 5 --body 'add || true to the script'", "allow", "R1"),
    ('gh pr comment 5 --body ">/dev/null hides output"', "allow", "R1"),
    ('git push origin feature/x || { echo "push failed" >&2; exit 1; }', "allow", "R1"),
    (
        (
            "cat > /example/b.md <<'EOF'\nquote 2>/dev/null and || true\nEOF\n"
            "gh issue create --title x --body-file /example/b.md"
        ),
        "allow",
        "1628",
    ),
    ("gh pr edit 5 --body-file - <<'EOF'\nthe || echo form\nEOF", "allow", "1628"),
    ("git commit -F - <<'EOF'\nsay 2>/dev/null\nEOF\ngit push origin feature/x", "allow", "1628"),
    (
        "cat > /example/b.md <<'EOF'\nx\nEOF\ngh pr edit 5 --body-file /example/b.md || :",
        "deny",
        "1628",
    ),
    ("git fetch origin >/dev/null 2>&1", "allow", "R1"),
    (_STRANGER_REPLY + ' -F t="PRRT_kwDOExample12345" >/dev/null 2>&1 || true', "deny", "incident"),
    (_STRANGER_REPLY + ' -F t="PRRT_kwDOExample12345"', "deny", "R2"),
    (_STRANGER_REPLY + ' -F t="$TID"', "allow", "R2"),
    (_ID_IN_QUERY, "deny", "R2"),
    ("gh api graphql -f query='{node(id:\"PR_kwDOExample12345\"){id}}'", "allow", "R2"),
    ("gh api repos/example/repo/actions/variables -f name=AWS_SECRET_ACCESS_KEY", "allow", "R2"),
    ("gh api graphql -f query='mutation($b:String!){x(b:$b)}' -f b=fixed_the_thing", "allow", "R2"),
    ("gh issue comment 5 -R stranger/x --body hi", "deny", "incident"),
    ("gh issue comment 5 --repo=stranger/x --body hi", "deny", "R3"),
    ("gh issue comment 5 -Rstranger/x --body hi", "deny", "R3"),
    ("gh -R stranger/x issue comment 5 --body hi", "deny", "R3"),
    ('gh issue comment 5 --repo "stranger/x" --body hi', "deny", "R3"),
    ("gh issue comment 5 --repo github.com/stranger/x --body hi", "deny", "R3"),
    ("gh issue comment 5 --repo example/other --body hi", "allow", "R3"),
    ("gh pr create -f -R stranger/x", "deny", "R3"),
    ('gh repo edit --description "CI/CD helpers"', "allow", "R3"),
    (
        "gh project item-add 3 --owner @me --url https://github.com/example/repo/issues/5",
        "allow",
        "R3",
    ),
    ("gh issue comment 5 --repo Example/Repo --body hi", "allow", "R3"),
    ("gh api repos/stranger/x/issues/1/comments -f body=hi", "deny", "R3"),
    ("gh api /repos/stranger/x/issues/1/comments -f body=hi", "deny", "R3"),
    ("gh api repos/stranger/x/issues/1", "allow", "R3"),
    ("gh api repos/{owner}/{repo}/issues/1/comments -f body=hi", "allow", "R3"),
    ("gh api repos/example/other/issues/1/comments -f body=hi", "allow", "R3"),
    ("GH_REPO=stranger/x gh issue comment 5 --body hi", "deny", "R3"),
    ("export GH_REPO=stranger/x && gh issue comment 5 --body hi", "deny", "R3"),
    ("GH_REPO=stranger/x; gh issue comment 5 --body hi", "deny", "R3"),
    ("env GH_REPO=stranger/x gh issue comment 5 --body hi", "deny", "R3"),
    ("gh pr comment https://github.com/stranger/x/pull/5 --body hi", "deny", "R3"),
    ("gh issue comment https://github.com/stranger/x/issues/5 --body hi", "deny", "R3"),
    ("gh pr comment https://github.com/example/other/pull/5 --body hi", "allow", "R3"),
    ("gh repo edit stranger/x --description y", "deny", "R3"),
    ("gh repo delete stranger/x --yes", "deny", "R3"),
    ("gh issue transfer 5 stranger/x", "deny", "R3"),
    ("gh project create --owner stranger --title y", "deny", "R3"),
    ("gh secret set TOKEN --org stranger --body x", "deny", "R3"),
    ("gh variable set NAME -o stranger --body x", "deny", "R3"),
    ('gh repo edit -d "CI/CD helpers"', "allow", "R3"),
    ("gh issue close 5 --comment https://github.com/stranger/x/pull/9", "allow", "R3"),
    ('gh issue close --comment "x" https://github.com/stranger/x/issues/5', "deny", "R3"),
    ('gh pr close -c "x" https://github.com/stranger/x/pull/5', "deny", "R3"),
    ("gh pr review 5 --comment -b looks-good", "allow", "R3"),
    ("gh pr review --comment https://github.com/stranger/x/pull/5 -b ok", "deny", "R3"),
    ("gh issue close 5 -c https://github.com/stranger/x/pull/9", "allow", "R3"),
    ('gh issue comment 5 --repo "$TARGET" --body hi', "allow", "R3 run-time target"),
    ("gh issue comment 5 --body 'see --repo stranger/x'", "allow", "R3"),
    ("gh issue view 5 --repo stranger/x", "allow", "R3"),
    ("gh repo clone stranger/x", "allow", "R3"),
    ("bash -c 'gh issue comment 5 --repo stranger/x --body hi'", "deny", "R3"),
    ("eval gh issue comment 5 --repo stranger/x --body hi", "deny", "R3"),
    ("gh issue comment 1 --repo stranger/x --body hi  # it's", "deny", "R3"),
    ("cat <<'EOF'\ngh issue comment 5 --repo stranger/x --body hi\nEOF", "allow", "R3"),
    ('echo "gh issue comment 5 --repo stranger/x"', "allow", "R3"),
    (
        "GH_WRITE_GUARD_ALLOW=stranger/x gh issue comment 5 -R stranger/x --body hi",
        "deny",
        "R3 inline grant",
    ),
    ("gh pr merge 5 --admin --squash", "deny", "R4"),
    ("gh pr merge 5 --squash", "allow", "R4"),
    ("gh pr merge 5 -m --admin", "deny", "R4"),
    ("git commit --no-verify -m x", "deny", "R4"),
    ("git commit -n -m x", "deny", "R4"),
    ("git commit -an -m x", "deny", "R4"),
    ("git commit -mn", "allow", "R4"),
    ("git push --no-verify origin feature/x", "deny", "R4"),
    ("git push -n origin feature/x", "allow", "R4"),
    ("git -c core.hooksPath=/dev/null commit -m x", "deny", "1127"),
    ("git -c core.hooksPath=/tmp/none push origin feature/x", "deny", "1127"),
    ("git -c include.path=/tmp/x.cfg commit -m x", "deny", "R4"),
    ("git -c core.hooksPath=/dev/null status", "allow", "R4"),
    ("git -c user.name=x commit -m y", "allow", "R4"),
    ("bash -c 'git commit --no-verify -m x'", "deny", "R4"),
    ('git commit -m "skip --no-verify next time"', "allow", "R4"),
    (_RESOLVE, "deny", "R5"),
    ("gh api repos/example/repo/pulls/5/comments/123/replies -f body=x", "deny", "R5"),
    ("gh api repos/example/repo/pulls/5/comments/123/replies", "allow", "R5"),
    (
        "scripts/pr_review.py reply 5 --repo example/repo --match x --body y --resolve",
        "allow",
        "R5",
    ),
    ("until gh pr checks 5; do sleep 30; done", "deny", "incident"),
    ("while true; do sleep 5; done", "deny", "R7"),
    (
        "while ! gh pr view 5 --json state | grep -q MERGED\ndo\n  sleep 30\ndone",
        "deny",
        "R7",
    ),
    ("while sleep 30; do gh pr checks 5 && break; done", "deny", "R7"),
    ("for ((;;)); do sleep 1; done", "deny", "R7"),
    ("bash -c 'until gh pr checks 5; do sleep 30; done'", "deny", "R7"),
    ("eval 'while true; do sleep 1; done'", "deny", "R7"),
    ("nohup bash -c 'while true; do sleep 60; done' &", "deny", "R7"),
    ("timeout 600 bash -c 'until x; do sleep 30; done &'", "deny", "R7"),
    ('while [ "$i" -lt 20 ]; do while true; do sleep 1; done; done', "deny", "R7"),
    ("while true; do for i in 1 2; do sleep 1; done; done", "deny", "R7"),
    ("timeout 600 bash -c 'until gh pr checks 5; do sleep 30; done'", "allow", "R7"),
    ("timeout 600 bash -c 'while true; do sleep 60; done' &", "allow", "R7"),
    ('i=0; while [ "$i" -lt 20 ]; do sleep 5; i=$((i+1)); done', "allow", "R7"),
    ("while (( SECONDS < 600 )); do sleep 5; done", "allow", "R7"),
    ("while read -r line; do sleep 1; done < list.txt", "allow", "R7"),
    ("for i in 1 2 3; do sleep 1; done", "allow", "R7"),
    ("for ((i=0; i<5; i++)); do sleep 1; done", "allow", "R7"),
    ("while true; do gh pr checks 5 && break; done", "allow", "R7"),
    ("cat <<'EOF' > notes.md\nuntil x; do sleep 30; done\nEOF", "allow", "R7"),
    ('echo "while true; do sleep 1; done"', "allow", "R7"),
    ("cat > /example/notes.md <<'EOF'\nas in `NAME=value git ...`\nEOF", "allow", "3380"),
    ("find . -name '*.txt' | xargs -I{} sh -c '{}'", "allow", "3385"),
    ("printf 5 | xargs gh pr comment --body x", "allow", "R3"),
    ("env -S 'gh issue comment 5 -R stranger/x --body hi'", "deny", "3716"),
    ("env --split-string='gh issue comment 5 -R stranger/x' -b hi", "deny", "3716"),
    ("env -iS'gh issue comment 5 -R stranger/x --body hi'", "deny", "3716"),
    ("env -a x gh issue comment 5 -R stranger/x --body hi", "deny", "3716"),
]

_ENV_CASES: list[tuple[str, dict[str, str], str, str]] = [
    ("gh issue comment 5 -R stranger/x --body hi", {_ALLOW_ENV: "stranger/x"}, "allow", "R3"),
    ("gh issue comment 5 -R stranger/x --body hi", {_ALLOW_ENV: "stranger/*"}, "allow", "R3"),
    ("gh issue comment 5 -R stranger/x --body hi", {_ALLOW_ENV: "a/b, stranger/x"}, "allow", "R3"),
    ("gh issue comment 5 -R stranger/x --body hi", {_ALLOW_ENV: "stranger/y"}, "deny", "R3"),
    ("gh project create --owner stranger --title y", {_ALLOW_ENV: "stranger/*"}, "allow", "R3"),
    ("gh issue comment 5 --body hi", {"GH_REPO": "stranger/x"}, "deny", "R3"),
    ("gh issue comment 5 -R example/repo --body hi", {"GH_REPO": "stranger/x"}, "allow", "R3"),
    (_RESOLVE, {_ALLOW_ENV: "stranger/x"}, "allow", "R5"),
    (
        "gh api repos/stranger/x/pulls/5/comments/123/replies -f body=x",
        {_ALLOW_ENV: "stranger/x"},
        "allow",
        "R5",
    ),
    (
        "gh api repos/example/repo/pulls/5/comments/123/replies -f body=x",
        {_ALLOW_ENV: "stranger/x"},
        "deny",
        "R5",
    ),
    ("env -u GH_REPO gh issue comment 5 --body hi", {"GH_REPO": "stranger/x"}, "allow", "3716"),
    ("env -i gh issue comment 5 --body hi", {"GH_REPO": "stranger/x"}, "allow", "3716"),
]

_RULES: dict[str, set[str] | None] = {
    "develop": {"pull_request"},
    "main": {"non_fast_forward", "deletion"},
}
_UNREADABLE: dict[str, set[str] | None] = {"develop": None, "feature/x": None}

_PUSH_CASES: list[tuple[str, dict[str, set[str] | None], str, str, str]] = [
    ("git push origin develop", _RULES, "feature/x", "deny", "R4"),
    ("git push origin feature/x", _RULES, "feature/x", "allow", "R4"),
    ("git push -u origin feature/x", _RULES, "feature/x", "allow", "R4"),
    ("git push origin HEAD:develop", _RULES, "feature/x", "deny", "R4"),
    ("git push origin feature/x:refs/heads/develop", _RULES, "feature/x", "deny", "R4"),
    ("git push --force origin feature/x", _RULES, "feature/x", "allow", "R4"),
    ("git push origin main", _RULES, "feature/x", "allow", "R4"),
    ("git push origin +main", _RULES, "feature/x", "deny", "R4"),
    ("git push -f origin main", _RULES, "feature/x", "deny", "R4"),
    ("git push origin :main", _RULES, "feature/x", "deny", "R4"),
    ("git push origin --delete main", _RULES, "feature/x", "deny", "R4"),
    ("git push origin --delete feature/example", _RULES, "feature/x", "allow", "3314"),
    ("git push origin :feature/example", _RULES, "feature/x", "allow", "3314"),
    ("git push", _RULES, "develop", "deny", "R4"),
    ("git push", _RULES, "feature/x", "allow", "R4"),
    ("git push origin HEAD", _RULES, "develop", "deny", "R4"),
    ("git push origin --tags", _RULES, "develop", "allow", "R4"),
    ("git push --all origin", _RULES, "feature/x", "deny", "R4"),
    ("git push --mirror origin", _RULES, "feature/x", "deny", "R4"),
    ("git push origin develop", {"develop": set()}, "feature/x", "allow", "R4"),
    ("git push origin develop", _UNREADABLE, "feature/x", "deny", "R4"),
    ("git push origin feature/x", _UNREADABLE, "feature/x", "allow", "R4"),
    ('git push origin "$BRANCH"', _UNREADABLE, "feature/x", "allow", "R4 run-time refspec"),
    ("cd /c/work/wt && git push origin develop", _RULES, "feature/x", "deny", "R4"),
    ("timeout 60 git push origin develop", _RULES, "feature/x", "deny", "R4"),
    ("bash -c 'git push origin develop'", _RULES, "feature/x", "deny", "R4"),
    ("xargs -I{} sh -c 'git push origin develop'", _RULES, "feature/x", "deny", "R4"),
    (_COMMIT_THEN_PUSH, _RULES, "feature/x", "deny", "R4"),
]

_CHECKOUT_CASES: list[tuple[str, str, dict[str, str], str, str]] = [
    ("git commit -m x", _PRIMARY, {}, "deny", "incident"),
    ("git add automations.yaml", _PRIMARY, {}, "deny", "3200"),
    ("git commit -m x", _PRIMARY, {_ALLOW_PRIMARY_ENV: "1"}, "allow", "R6"),
    ("git commit -m x", _PRIMARY, {_ALLOW_PRIMARY_ENV: "false"}, "deny", "R6"),
    ("git reset --hard", _PRIMARY, {}, "deny", "R6"),
    ("git stash", _PRIMARY, {}, "deny", "R6"),
    ("git stash list", _PRIMARY, {}, "allow", "R6"),
    ("git clean -fd", _PRIMARY, {}, "deny", "R6"),
    ("git clean -nfd", _PRIMARY, {}, "allow", "R6"),
    ("git checkout develop", _PRIMARY, {}, "allow", "R6"),
    ("git checkout -", _PRIMARY, {}, "allow", "R6"),
    ("git checkout .", _PRIMARY, {}, "deny", "R6"),
    ("git checkout -- README.md", _PRIMARY, {}, "deny", "R6"),
    ("git checkout -b topic", _PRIMARY, {}, "deny", "R6"),
    ("git switch -C develop", _PRIMARY, {}, "deny", "R6"),
    ("git merge --ff-only origin/develop", _PRIMARY, {}, "allow", "R6"),
    ("git pull --ff-only", _PRIMARY, {}, "allow", "R6"),
    ("git pull", _PRIMARY, {}, "deny", "R6"),
    ("git worktree add ../wt-x -b x origin/develop", _PRIMARY, {}, "allow", "R6"),
    ("git worktree remove ../wt-x", _PRIMARY, {}, "allow", "R6"),
    ("git worktree remove -f ../wt-x", _PRIMARY, {}, "deny", "R6"),
    ("git fetch origin", _PRIMARY, {}, "allow", "R6"),
    ("git status", _PRIMARY, {}, "allow", "R6"),
    ("git push origin feature/x", _PRIMARY, {}, "deny", "R6"),
    ("git push origin --delete feature/x", _PRIMARY, {}, "allow", "1642"),
    ("git push origin :feature/x", _PRIMARY, {}, "allow", "1642"),
    ("git push my_fork-2 :feature/x", _PRIMARY, {}, "allow", "1642"),
    ("git push -dq origin feature/x feature/y", _PRIMARY, {}, "allow", "1642"),
    ("git push --dry-run origin :refs/heads/feature/x", _PRIMARY, {}, "allow", "1642"),
    (
        (
            "git ls-remote --heads --exit-code -- origin refs/heads/feature/x "
            "&& git push origin --delete -- feature/x"
        ),
        _PRIMARY,
        {},
        "allow",
        "1642",
    ),
    ("git push origin --delete develop", _PRIMARY, {}, "deny", "1642"),
    ("git push origin :refs/heads/main", _PRIMARY, {}, "deny", "1642"),
    ("git push origin --delete feature/x develop", _PRIMARY, {}, "deny", "1642"),
    ("git push origin :feature/x feature/y", _PRIMARY, {}, "deny", "1642"),
    ("git push origin +:feature/x", _PRIMARY, {}, "deny", "1642"),
    ("git push -f origin :feature/x", _PRIMARY, {}, "deny", "1642"),
    ("git push -u origin --delete feature/x", _PRIMARY, {}, "deny", "1642"),
    ("git push --prune origin :feature/x", _PRIMARY, {}, "deny", "1642"),
    ("git push origin :", _PRIMARY, {}, "deny", "1642"),
    ("git push origin :refs/tags/v1", _PRIMARY, {}, "deny", "1642"),
    ("git push origin --delete :feature/x", _PRIMARY, {}, "deny", "1642"),
    ("git push --delete origin", _PRIMARY, {}, "deny", "1642"),
    ("git push . --delete feature/x", _PRIMARY, {}, "deny", "1642"),
    ("git push ../other :feature/x", _PRIMARY, {}, "deny", "1642"),
    ('git push origin --delete "$BRANCH"', _PRIMARY, {}, "deny", "1642"),
    ("git -c core.sshCommand=ssh push origin :feature/x", _PRIMARY, {}, "deny", "1642"),
    ("cd /c/work/wt && git commit -m x", _PRIMARY, {}, "allow", "R6"),
    ("git commit -m x", _WORKTREE, {}, "allow", "R6"),
    ("cd /c/work/primary && git commit -m x", _WORKTREE, {}, "deny", "incident"),
    ("cd /c/work/primary/sub && git add f", _WORKTREE, {}, "deny", "R6"),
    ("cd /c/work/primary && git status", _WORKTREE, {}, "allow", "R6"),
    ("(cd /c/work/primary && git log -1) && git commit -m x", _WORKTREE, {}, "allow", "R6"),
    ("git -C /c/work/primary commit -m x", _WORKTREE, {}, "deny", "R6"),
    ("pushd /c/work/primary; git add f", _WORKTREE, {}, "deny", "R6"),
    ("bash -c 'cd /c/work/primary && git reset --hard'", _WORKTREE, {}, "deny", "R6"),
    ('git -C "$D" commit -m x', _WORKTREE, {}, "allow", "3382"),
    ("git -C /elsewhere commit -m x", _WORKTREE, {}, "allow", "R6"),
    ("env -C /c/work/primary git commit -m x", _WORKTREE, {}, "deny", "3716"),
    ("env --chdir=/c/work/primary sh -c 'git add f'", _WORKTREE, {}, "deny", "3716"),
    ("env --chdir /c/work/wt git commit -m x", _PRIMARY, {}, "allow", "3716"),
]

# A directory that does not exist, so no origin and no branch rules resolve there.
_NO_ORIGIN_CASES: list[tuple[str, str, str]] = [
    ("git push origin develop", "deny", "R4"),
    ("git push origin feature/x", "allow", "R4"),
    ("gh issue comment 5 -R stranger/x --body hi", "allow", "R3"),
]


def _fixture_lookup(directory: str) -> bool | None:
    """The fixture's primary-checkout answer for a directory under one of its checkouts."""
    d = directory.replace("\\", "/")
    for root, primary in (
        (_msys_drive_path(_PRIMARY) or "", True),
        (_msys_drive_path(_WORKTREE) or "", False),
    ):
        if d == root or d.startswith(root + "/"):
            return primary
    return None


_SELFTEST_GIT = (
    *("git", "-c", "commit.gpgsign=false", "-c", "user.name=Example"),
    *("-c", "user.email=example@example.com"),
)
_GIT_LOCATION_VARS = (
    *("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"),
    *("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES"),
    *("GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_NAMESPACE", "GIT_PREFIX"),
)


@contextlib.contextmanager
def _git_location_cleared() -> Iterator[None]:
    """Remove each variable that names a repository from the environment, restoring them on exit."""
    cleared = {name: os.environ.pop(name) for name in _GIT_LOCATION_VARS if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(cleared)


def _build_selftest_repo(repo: str, steps: list[list[str]]) -> bool:
    """Create a repository at `repo` on `develop` with one empty commit, then run `steps` in it.

    It answers True only when every command exits 0, so a caller skips on a host that cannot build
    the repository.
    """
    commands = [
        [*_SELFTEST_GIT, "init", "-q", "-b", "develop", repo],
        [*_SELFTEST_GIT, "-C", repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", "init"],
        *([*_SELFTEST_GIT, "-C", repo, *step] for step in steps),
    ]
    for command in commands:
        try:
            if subprocess.run(command, capture_output=True, check=False).returncode != 0:
                return False
        except OSError:
            return False
    return True


def _is_primary_checkout_selftest() -> bool | str:
    """Tell a real primary checkout from its linked worktree, the primary's name not valid UTF-8.

    "skip" where the case could not run, which is neither a pass nor a failure.
    That happens because Windows refuses such a name, and a host can have no git.
    """
    with tempfile.TemporaryDirectory() as tmp:
        try:
            primary = os.path.join(tmp, os.fsdecode(b"checkout_\xe9"))
            os.mkdir(primary)
        except (OSError, UnicodeError):
            return "skip"
        linked = os.path.join(tmp, "linked")
        if not _build_selftest_repo(primary, [["worktree", "add", "-q", linked, "-b", "x"]]):
            return "skip"
        return _is_primary_checkout(primary) is True and _is_primary_checkout(linked) is False


_PUSH_REPO_CASES: list[tuple[str, str | None]] = [
    ("git push origin :feature/x", "example"),
    ("git --git-dir={b}/.git push origin :feature/x", "stranger"),
    ("git --git-dir {b}/.git push origin :feature/x", "stranger"),
    ("git -C {b} push origin :feature/x", "stranger"),
    ("git -C {b} --git-dir=.git push origin :feature/x", "stranger"),
    ("git --work-tree={b} push origin :feature/x", "example"),
    ("GIT_DIR={b}/.git git push origin :feature/x", "stranger"),
    ("env GIT_DIR={b}/.git git push origin :feature/x", "stranger"),
    ('cd "$X" && git --git-dir={b}/.git push origin :feature/x', "stranger"),
    ("export GIT_DIR={b}/.git; git push origin :feature/x", None),
    ("GIT_DIR={b}/.git; git push origin :feature/x", None),
    ('git --git-dir="$D" push origin :feature/x', None),
    ("git -c remote.origin.pushurl=https://github.com/x/y push origin :feature/x", None),
    ("GIT_CONFIG_COUNT=1 git push origin :feature/x", None),
    ("git --git-dir={b}/.git push https://github.com/example/x :feature/x", "example"),
    ("env --chdir={b} GIT_DIR=.git git push origin :feature/x", "stranger"),
    ("env --chdir=../b GIT_DIR=.git git push origin :feature/x", "stranger"),
    ("env -C {b} git push origin :feature/x", "stranger"),
    ("env -iC{b} git push origin :feature/x", "stranger"),
    ("env --ch {b} git push origin :feature/x", "stranger"),
    ("env -C {b} sh -c 'git push origin :feature/x'", "stranger"),
    ('env -C "$D" git push origin :feature/x', None),
    ("GIT_DIR={b}/.git env -i git push origin :feature/x", "example"),
    ("GIT_DIR={b}/.git env -u GIT_DIR git push origin :feature/x", "example"),
    ("export GIT_DIR={b}/.git; env --unset=GIT_DIR git push origin :feature/x", "example"),
    ("env -S 'GIT_DIR={b}/.git git push origin :feature/x'", "stranger"),
]
_PUSH_BRANCH_CASES: list[tuple[str, str | None]] = [
    ("git push origin", "feature/a"),
    ("git --git-dir={b}/.git push origin", "develop"),
    ("GIT_DIR={b}/.git git push origin", "develop"),
    ('cd "$X" && git --git-dir={b}/.git push origin', "develop"),
    ("GIT_DIR={b}/.git; git push origin", None),
    ('git --git-dir="$D" push origin', None),
    ("env -C {b} git push origin", "develop"),
    ('env -C "$D" git push origin', None),
]


def _push_repo_selftest() -> bool | str:
    """Read the repository each push writes from two real checkouts, their origins under different owners.

    Each case is a command run in checkout `a`, with `b` beside it, and the owner or the branch it
    writes. Checkout `a` is on `feature/a` and `b` on `develop`.
    "skip" where the checkouts could not be built, which is neither a pass nor a failure.
    """
    with tempfile.TemporaryDirectory() as tmp:
        a, b = (os.path.join(tmp, name).replace("\\", "/") for name in ("a", "b"))
        for repo, owner, steps in (
            (a, "example", [["switch", "-q", "-c", "feature/a"]]),
            (b, "stranger", []),
        ):
            url = f"https://github.com/{owner}/x.git"
            if not _build_selftest_repo(repo, [["remote", "add", "origin", url], *steps]):
                return "skip"
        ok = True
        for cmd, want in _PUSH_REPO_CASES:
            reading = _Reading()
            _read(cmd.format(b=b), a, {}, reading)
            c = reading.cmds[-1]
            git = _git(c.argv)
            got = _push_repo(c, git, _push_targets(git.args, lambda: None)[0])
            passed = (got[0] if got else None) == want
            ok = ok and passed
            print(f"  {'ok  ' if passed else 'FAIL'} [repo ] want={want} R4: {cmd}")
        for cmd, branch in _PUSH_BRANCH_CASES:
            reading = _Reading()
            _read(cmd.format(b=b), a, {}, reading)
            c = reading.cmds[-1]
            git = _git(c.argv)
            named = _named_git_dir(c, git)
            bare = _bare_push_branch(c.cwd, git.c_dirs, named, None, c.cwd is None)
            passed = (bare if isinstance(bare, str) else None) == branch
            ok = ok and passed
            print(f"  {'ok  ' if passed else 'FAIL'} [branch] want={branch} R4: {cmd}")
        return ok


def _selftest() -> int:
    """Run every self-test case with no variable naming a repository in the environment."""
    with _git_location_cleared():
        return _selftest_cases()


def _selftest_cases() -> int:
    ok = True

    def check(cmd: str, want: str, source: str, **seams: object) -> None:
        nonlocal ok
        args: dict[str, object] = {
            "cwd": _WORKTREE,
            "origin": _ORIGIN,
            "current_branch": "feature/x",
            "rules_lookup": lambda _b: set(),
            "environ": {},
            "primary_checkout_lookup": _fixture_lookup,
            "ref_resolver": lambda _d, r: r in _REFS,
        }
        args.update(seams)
        got, _ = classify(cmd, **args)  # type: ignore[arg-type]
        ok = ok and got == want
        shown = cmd.replace("\n", "\\n")
        print(f"  {'ok  ' if got == want else 'FAIL'} [{got:5}] want={want:5} {source}: {shown}")

    for cmd, want, source in _CASES:
        check(cmd, want, source)
    for cmd, environ, want, source in _ENV_CASES:
        check(cmd, want, source, environ=environ)
    for cmd, rules, branch, want, source in _PUSH_CASES:
        check(
            cmd,
            want,
            source,
            current_branch=branch,
            rules_lookup=lambda b, r=rules: r.get(b, set()),
        )
    for cmd, cwd, environ, want, source in _CHECKOUT_CASES:
        check(cmd, want, source, cwd=cwd, environ=environ)
    with tempfile.TemporaryDirectory() as tmp:
        absent = os.path.join(tmp, "absent")
        for cmd, want, source in _NO_ORIGIN_CASES:
            check(cmd, want, source, cwd=absent, origin=None, rules_lookup=None)
    repos = _push_repo_selftest()
    ok = ok and repos in (True, "skip")
    if repos == "skip":
        print("  ok   [repo ] the push-repository checkouts could not be built: skip")
    live = _is_primary_checkout_selftest()
    ok = ok and live in (True, "skip")
    print(
        f"  {'FAIL' if live is False else 'ok  '} [live ] a real checkout reads as primary: {live}"
    )
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


def _main() -> None:
    try:
        data = json.load(sys.stdin)
    # The hook never crashes on input it does not recognize.
    except Exception:  # noqa: BLE001
        sys.exit(0)
    # A non-zero exit is a hook error, which lets the call proceed, so an unknown shape allows on purpose.
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        sys.exit(0)
    tool_input = data.get("tool_input")
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    cwd = data.get("cwd")
    decision, reason = classify(
        cmd if isinstance(cmd, str) else "", cwd if cwd and isinstance(cwd, str) else os.getcwd()
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
