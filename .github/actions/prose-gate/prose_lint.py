#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py>=4.2.0", "pygments>=2.21.0"]
# ///
"""Enforce the GOVERNANCE.md "Documentation Style Conventions" rules no other linter checks.

Three layers:

- Extract text units through Pygments and markdown-it-py.
- Normalize them per language from the `prose-gate.json` beside this script.
- Evaluate each rule in `RULE_TABLE` on a unit.

The JSON holds the language data, each rule's message and default, and the vocabulary and
exemptions.

A repository may carry `.prose-gate.json` at its root with additions to the carried exemptions and
nothing else. Each is a list merged over the carried one:

- `hub_hosted` and `home_path_exempt` take paths.
- `generated_trees` takes directory names.
- `service_accounts` takes account names.
- `dupword_allow` and `spelling_allow` take phrases and words.
- `tool_directives` takes directive names matched literally.

Any other key is refused, since an override that silently does nothing reads as one that works.

Exit 1 on a finding, 2 when the run cannot be scoped. Read-only. Every git read names its encoding,
with surrogateescape, so a byte the locale cannot map neither ends the read nor changes a name.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import re
import subprocess
import sys
import unicodedata
from bisect import bisect_right
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple

MISSING = ""
try:
    from markdown_it import MarkdownIt
    from markdown_it.common.html_re import HTML_TAG_RE
    from markdown_it.token import Token
    from pygments.lexer import Lexer, RegexLexer, bygroups
    from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
    from pygments.lexers.dotnet import CSharpLexer
    from pygments.lexers.shell import BashLexer
    from pygments.token import Comment, Keyword, Name, Punctuation, String, Text, Whitespace
    from pygments.util import ClassNotFound
except ImportError as missing:
    MISSING = f"{missing.name} is not installed"
DATA_PATH = Path(__file__).resolve().with_name("prose-gate.json")
if MISSING or not DATA_PATH.is_file():
    # Printed rather than passed to `sys.exit`, since a stale in-process caller drops that message.
    print(
        f"prose_lint.py: {MISSING or 'prose-gate.json is not beside the script'}. The gate runs as "
        "`uv run --no-project --script` beside its data file, and a hook importing it in-process "
        "carries a stale hub_fetch_run.py snippet to resync from the hub.",
        file=sys.stderr,
    )
    sys.exit(1)
BASE: dict[str, Any] = json.loads(DATA_PATH.read_text(encoding="utf-8"))
OVERRIDE_NAME = ".prose-gate.json"
RULES: dict[str, str] = {name: rule["summary"] for name, rule in BASE["rules"].items()}
DEFAULT_RULES = frozenset(name for name, rule in BASE["rules"].items() if rule["default"])
MESSAGES: dict[str, str] = BASE["messages"]
SENTENCE_WORD_CAP: int = BASE["sentence_word_cap"]
# The data file names each character as `U+XXXX`, which keeps the file itself ASCII.
TIER1 = {chr(int(k.removeprefix("U+"), 16)): v for k, v in BASE["charset"]["tier1"].items()}
TIER2 = {chr(int(k.removeprefix("U+"), 16)): v for k, v in BASE["charset"]["tier2"].items()}
TIER3 = frozenset(chr(int(k.removeprefix("U+"), 16)) for k in BASE["charset"]["tier3"])
TIER4 = frozenset(chr(int(k.removeprefix("U+"), 16)) for k in BASE["charset"]["tier4"])
VARIATION_SELECTOR_16 = chr(0xFE0F)
GENERATED_ROOTS = frozenset(BASE["generated_roots"])
INSTRUCTION_DOCS = frozenset(BASE["instruction_docs"])
DUP_ALLOW = frozenset(BASE["exemptions"]["dupword_allow"])
# A floor on what a healthy sweep of this repo reaches, asserted by the tests.
LEAST_PLAUSIBLE = 60
# The label and the variable that stand `comment-added` down, and the values that read as off.
# The workflow and `repo-config/labels.json` spell the label out separately, which a case asserts.
COMMENT_LABEL_NAME = "comments"
COMMENT_ENV_NAME = "PROSE_ALLOW_COMMENTS"
COMMENT_ENV_OFF = frozenset({"", "0", "false", "no", "off"})
Finding = tuple[int, str, str]


def say(message: str, **fields: Any) -> str:
    """The data file's text for `message`, its fields filled in."""
    return MESSAGES[message].format(label=COMMENT_LABEL_NAME, env=COMMENT_ENV_NAME, **fields)


def finding(line: int, rule: str, message: str | None = None, **fields: Any) -> Finding:
    """A finding, its message the data file's text for `message`, or for the rule itself."""
    return line, rule, say(message or rule, **fields)


def british_spellings(spelling: dict[str, Any]) -> dict[str, str]:
    """Every banned spelling mapped to its US form, each family generating its own inflections."""
    words = dict(spelling["odd"])
    for stem in spelling["ise_stems"]:
        words.update({stem + gb: stem + us for gb, us in spelling["ise_endings"]})
    for stem in spelling["our_stems"]:
        words.update({f"{stem}our{end}": f"{stem}or{end}" for end in spelling["our_endings"]})
    for stem in spelling["re_stems"]:
        words.update({stem + gb: stem + us for gb, us in spelling["re_endings"]})
    return words


BRITISH = british_spellings(BASE["spelling"])


class Config(NamedTuple):
    """The carried data with a repository's exemptions merged in, and the patterns built from it."""

    exempt: dict[str, frozenset[str]]
    british: dict[str, str]
    british_re: re.Pattern[str]
    not_prose: re.Pattern[str]
    tool_directive: re.Pattern[str]


def build_config(data: dict[str, Any]) -> Config:
    exempt = {k: frozenset(v) for k, v in data["exemptions"].items()}
    british = {
        gb: us
        for gb, us in british_spellings(data["spelling"]).items()
        if gb not in exempt["spelling_allow"]
    }
    # Longest alternative first, so an inflection is read whole rather than as its base word.
    alternatives = "|".join(sorted(british, key=len, reverse=True))
    return Config(
        exempt,
        british,
        re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE),
        re.compile("^(?:" + "|".join(data["not_prose"]) + ")"),
        re.compile("^(?:" + "|".join(data["exemptions"]["tool_directives"]) + ")"),
    )


class OverrideError(ValueError):
    """A repository's `.prose-gate.json` that cannot be honored as written."""


def merged(base: dict[str, Any], override: object) -> dict[str, Any]:
    """The carried data with each override list added to its exemption, a directive name escaped."""
    if not isinstance(override, dict):
        raise OverrideError("is not a JSON object")
    if unknown := sorted(set(override) - set(BASE["override_keys"])):
        raise OverrideError(f"holds keys it may not: {quoted(unknown)}")
    out = json.loads(json.dumps(base))
    for key, vals in override.items():
        if not isinstance(vals, list) or not all(isinstance(v, str) and v.strip() for v in vals):
            raise OverrideError(f"holds {key!r} as other than a list of nonblank strings")
        add = [re.escape(v) for v in vals] if key == "tool_directives" else vals
        out["exemptions"][key] = [*out["exemptions"][key], *add]
    return out


@functools.cache
def config_for(root: str | None) -> Config:
    """The configuration a scan of `root` reads, its `.prose-gate.json` merged where one exists."""
    path = Path(root) / OVERRIDE_NAME if root else None
    if path is None or not path.is_file():
        return build_config(BASE)
    try:
        override = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OverrideError(f"cannot be read as JSON: {exc}") from exc
    return build_config(merged(BASE, override))


# File scope and diff handling.


def rel(path: Path) -> str:
    """The repo-relative posix key a git diff uses, keeping the leading dot of a dotfile."""
    return path.as_posix().removeprefix("./")


def repo_key(path: Path, root: Path) -> str:
    """The repository-relative key joining a file to a diff entry, or its own where it is outside."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return rel(path)


def git_text(*args: str, env: dict[str, str] | None = None) -> str | None:
    """A git command's standard output, or None where git cannot run or fails."""
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
            env=env,
        )
    except (OSError, ValueError):
        return None
    return r.stdout if r.returncode == 0 else None


def git_bytes(root: Path, args: str, base: str) -> str | None:
    """A diff against `base`, captured as bytes so a lone carriage return breaks no line."""
    try:
        cmd = ["git", "-C", str(root), *args.split(), base, "--"]
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return raw.decode("utf-8", "surrogateescape")


def untracked_paths(root: Path) -> list[str]:
    """Paths under `root` that git holds no history for and is not ignoring, which no diff names."""
    out = git_text("-C", str(root), "ls-files", "-z", "--others", "--exclude-standard")
    return [name for name in (out or "").split("\0") if name]


def diff_header_path(field: str) -> str | None:
    """The path a `+++` header names, its C-quoted form decoded, or None where it names no file."""
    field = field.split("\t", 1)[0]
    if field.startswith('"') and field.endswith('"') and len(field) > 1:
        # Latin-1 round-trips each byte, so a raw byte the escape carries survives the decode.
        unescaped = field[1:-1].encode("latin-1", "backslashreplace").decode("unicode-escape")
        field = unescaped.encode("latin-1", "surrogateescape").decode("utf-8", "surrogateescape")
    return field[2:] if field.startswith("b/") else None


def undiffable_paths(base: str, root: Path) -> set[str] | None:
    """Paths git declines to diff against `base`, as `--numstat -z` names them, None if git fails."""
    raw = git_bytes(root, "diff --numstat -z --no-ext-diff --no-textconv", base)
    if raw is None:
        return None
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    paths: set[str] = set()
    i = 0
    while i < len(fields):
        added, _, rest = fields[i].partition("\t")
        removed, _, path = rest.partition("\t")
        i += 1
        if not path:
            # A stream ending between a rename's two paths is refused rather than parsed on.
            if i + 1 >= len(fields):
                return None
            path, i = fields[i + 1], i + 2
        if added == "-" and removed == "-":
            paths.add(path)
    return paths


# Each option pins a setting a host may change, quoting and prefixes and hunk context among them.
# The external and textconv drivers would otherwise replace the body this parse reads.
DIFF_ARGS = (
    "-c core.quotePath=true diff --unified=0 --inter-hunk-context=0 --no-color "
    "--ignore-cr-at-eol --src-prefix=a/ --dst-prefix=b/ --no-ext-diff --no-textconv"
)


def changed_lines(base: str, root: Path) -> dict[str, set[int]] | None:
    """Map path -> lines this tree adds vs `base`, an untracked or undiffable text file in full."""
    raw = git_bytes(root, DIFF_ARGS, base)
    undiffable = undiffable_paths(base, root) if raw is not None else None
    if raw is None or undiffable is None:
        return None
    out: dict[str, set[int]] = {}
    cur = None
    # A header is honored only in the block a `diff --git` line opens, which content cannot forge.
    in_header = False
    for line in raw.split("\n"):
        if line.startswith("diff --git "):
            in_header, cur = True, None
        elif in_header and line.startswith("+++ "):
            in_header = False
            cur = diff_header_path(line[4:])
            if cur is not None and (root / cur).is_file() and not is_text(root / cur):
                cur = None
            if cur is not None:
                out.setdefault(cur, set())
        elif line.startswith("@@") and cur:
            in_header = False
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                out[cur].update(range(start, start + int(m.group(2) or 1)))
    for name in [*undiffable, *untracked_paths(root)]:
        if is_text(root / name):
            out[name] = set(range(1, len((root / name).read_bytes().splitlines()) + 1))
    return out


def asked_about(key: str, paths: list[str]) -> bool:
    """Whether a repository-relative diff key falls under one of the requested paths."""
    for raw in paths:
        r = rel(Path(raw))
        # A separator is appended, so `catalog` cannot claim `catalogue/x.md`.
        if r in ("", ".") or key == r or key.startswith(r + "/"):
            return True
    return False


def unread_diff_files(
    scope: dict[str, set[int]], paths: list[str], excludes: tuple[str, ...], root: Path
) -> list[str]:
    """Files the diff names that this run was asked about and could have read, in sorted order."""
    asked = [repo_key(Path(p), root) for p in paths]
    trees = config_for(str(root)).exempt["generated_trees"]
    return [
        key
        for key in sorted(scope)
        if asked_about(key, asked)
        and not any(x in key for x in excludes)
        and trees.isdisjoint(Path(key).parts)
        and (root / key).is_file()
        and is_text(root / key)
    ]


def gate_provenance(explicit: str | None = None) -> str:
    """Which copy of this gate ran: the action's own pin, the environment's, or this checkout's."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("PROSE_GATE_PROVENANCE", "").strip()
    return env or checkout_provenance(Path(__file__).resolve())


GIT_LOCATION = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_WORK_TREE",
    }
)


def checkout_provenance(script: Path) -> str:
    """The commit describing this file, `-dirty` where edited, unknown where git tracks it not."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION}
    where = ("-C", str(script.parent))
    if git_text(*where, "ls-files", "--error-unmatch", "--", str(script), env=env) is None:
        return "unknown"
    head = (git_text(*where, "rev-parse", "--short", "HEAD", env=env) or "").strip()
    if not head:
        return "unknown"
    moved = (git_text(*where, "status", "--porcelain", "--", str(script), env=env) or "").strip()
    return f"local {head}-dirty" if moved else f"local {head}"


def scope_note(
    read: int,
    discovered: int,
    lines: int | None,
    base: str | None,
    provenance: str,
    plain: list[str] | None = None,
    undecoded: list[str] | None = None,
) -> str:
    """What the run read and which copy read it, files read as plain text or not at all named."""
    fields = {"read": read, "discovered": discovered, "lines": lines, "base": base}
    text = say("scope-whole" if base is None else "scope-diff", gate=provenance, **fields)
    for key, names in (("scope-plain", plain), ("scope-undecoded", undecoded)):
        text += say(key, count=len(names), paths=quoted(names, 5)) if names else ""
    return text


def quoted(paths: Iterable[Any], cap: int | None = None) -> str:
    """Paths as a sorted, quoted, comma-joined list, the first `cap` of them where one is given."""
    ordered = sorted(paths)
    shown = ordered if cap is None else ordered[:cap]
    more = " and more" if len(shown) < len(ordered) else ""
    return ", ".join(repr(str(p)) for p in shown) + more


CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
COMMAND_LEAD = re.compile(r"\A(\s*):(?=:)")
LEGACY_COMMAND = "##["


def printable(line: str) -> str:
    """`line` spelled as one line of output that runs no workflow command, a control escaped."""
    escaped = CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", line)
    escaped = COMMAND_LEAD.sub(lambda m: f"{m.group(1)}\\x3a", escaped)
    escaped = escaped.replace(LEGACY_COMMAND, "##\\x5b")
    return escaped.encode("utf-8", "backslashreplace").decode("utf-8")


def repo_root(path: Path) -> str:
    """The repository top level containing `path`, or '' when git cannot say."""
    start = path if path.is_dir() else path.parent
    return (git_text("-C", str(start), "rev-parse", "--show-toplevel") or "").strip()


def tracked_paths(root: Path) -> list[Path] | None:
    """Paths git tracks under `root`, or None when git cannot answer or answers with nothing."""
    out = git_text("-C", str(root), "ls-files", "-z")
    if out is None or not out.strip("\0"):
        return None
    return [root / name for name in out.split("\0") if name]


def walk_paths(root: Path) -> list[Path]:
    """Every file under `root` minus the generated roots, for a checkout git cannot describe."""
    return [
        p
        for p in root.rglob("*")
        if p.is_file() and not GENERATED_ROOTS.intersection(p.relative_to(root).parts)
    ]


def is_text(path: Path) -> bool:
    """A NUL byte in the first block marks a binary, the test the line-endings rule prescribes."""
    try:
        with path.open("rb") as fh:
            return b"\0" not in fh.read(8192)
    except OSError:
        return False


def shallow_checkout(root: Path) -> bool:
    """Whether the checkout at `root` is shallow, which holds no deletion history to key on."""
    answer = git_text("-C", str(root), "rev-parse", "--is-shallow-repository") or ""
    return answer.strip() == "true"


def discover(
    paths: list[str], excludes: tuple[str, ...] = (), root: Path | None = None
) -> list[Path]:
    """Every authored text file under the paths, scoped by git where git can describe the tree."""
    found: list[tuple[Path, str]] = []
    for raw in paths:
        p = Path(raw)
        base = p if p.is_dir() else (p.parent if p.is_file() else Path("."))
        anchor = root if root is not None else Path(repo_root(base) or base)
        trees = config_for(str(anchor)).exempt["generated_trees"]
        if p.is_file():
            found.append((p, repo_key(p, anchor)))
            continue
        candidates = tracked_paths(base)
        # Whether git can describe a tree is settled by asking git, never by its answer's size.
        if candidates is None and not repo_root(base):
            warning = f"warning: git cannot describe {base}, falling back to a filesystem walk"
            print(printable(warning), file=sys.stderr)
            candidates = walk_paths(base)
        else:
            candidates = (candidates or []) + [base / name for name in untracked_paths(base)]
        # A path named inside a generated tree asks for that tree.
        inside = not trees.isdisjoint(Path(repo_key(base, anchor)).parts)
        for q in candidates:
            key = repo_key(q, anchor)
            if inside or trees.isdisjoint(Path(key).parts):
                found.append((q, key))
    keep = {q for q, key in found if not any(x in key for x in excludes)}
    return sorted(q for q in keep if q.is_file() and is_text(q))


# Extract: a file becomes text units.


class Line(NamedTuple):
    """One source line of a unit, a comment's text without its marker and `raw` with it.

    `trailed` is a line where code follows the comment's closer, so deleting it deletes code.
    """

    number: int
    text: str
    leading: bool = True
    raw: str = ""
    help: bool = False
    trailed: bool = False


class Unit(NamedTuple):
    """A run of text one kind of rule reads.

    `source` is every line, and `prose` and `code` the unfenced ones. `paragraph` is a Markdown
    sentence block. `comment`, `doc`, and `markup` are runs of adjacent comment lines, `doc` a
    documentation comment and `markup` a Markdown HTML one. `docstring` opens at `column`.
    """

    kind: str
    lines: tuple[Line, ...]
    column: int = 0


# The units a file holds, its fenced lines, and whether it was read as plain text.
Extracted = tuple[list[Unit], frozenset[int], bool]


CODE_FENCE = re.compile(r"^\s*(```|~~~)")
SHEBANG = re.compile(r"^#!\s*(?:\S*/)?([\w.+-]+)(?:\s+(?:-\S+\s+|\w+=\S*\s+)*([\w.+-]+))?")


def language_of(lexer: str | None) -> dict[str, Any] | None:
    """The normalization data for a lexer, or None where the gate reads no comments through it."""
    return next((s for s in BASE["languages"].values() if lexer in s["lexers"]), None)


def lexer_name(path: Path, first: str) -> str | None:
    """The lexer a file reads through, `markdown`, or None for a format holding no comments.

    The data file's own mapping wins, then Pygments by file name, then the shebang.
    """
    files = BASE["files"]
    name, suffix = path.name.lower(), path.suffix.lower()
    if suffix in files["markdown"]:
        return "markdown"
    if name in files["name"] or suffix in files["plain"]:
        return files["name"].get(name)
    if suffix in files["suffix"]:
        return files["suffix"][suffix]
    for candidate in (path.name, path.name.lower()):
        with contextlib.suppress(ClassNotFound):
            return get_lexer_for_filename(candidate).aliases[0]
    if m := SHEBANG.match(first):
        program = m.group(2) if m.group(1) == "env" and m.group(2) else m.group(1)
        with contextlib.suppress(ClassNotFound):
            return get_lexer_by_name(program.rstrip("0123456789.")).aliases[0]
    return None if suffix else files["extensionless"]


# Pygments ends a heredoc at any later `STUB` in a `$STUB_DIR`, and reads no double-quoted label.
# This rule ends one only on its terminator line, tab-indented for `<<-`.
# A digit label is more often a shift, `(( x << 2 ))`, so only the last, unclosed branch drops it.
HEREDOC = (
    r"(?s)<<(-)?[ \t]*(['\"]?)\\?(?:(?=[A-Za-z_])())?(\w[\w-]*+)\2"
    r"(?:[^\n'\"#]*?(?<!<)<<(-)?[ \t]*(['\"]?)\\?(\w[\w-]*+)\6)?[^\n]*\n(?:(?:[^\n]*\n)*?(?(1)\t*)\4"
    r"(?=\n|\Z)()(?(7)(?:\n(?:[^\n]*\n)*?(?(5)\t*)\7(?=\n|\Z))?)|(?(3).*|(?!)))"
)
# Pygments reads a C# collection expression opening on its own line as an attribute ending at `]`.
# The comments inside it are lost, so an attribute here is read within its own line.
ATTRIBUTE = r"^([ \t]*)(\[[^\n]*?\])"


def patched(base: type[RegexLexer], stock: str, rule: tuple[str, Any] | None) -> type[Lexer]:
    """`base` with the rule it spells as `stock` replaced by `rule`, or removed where that is None."""

    def fix(states: dict[str, list[Any]]) -> dict[str, list[Any]]:
        hit = lambda r: isinstance(r, tuple) and r[0] == stock
        return {
            k: [rule if hit(r) else r for r in v if rule or not hit(r)] for k, v in states.items()
        }

    variants = getattr(base, "token_variants", False)
    tokens = {k: fix(v) for k, v in base.tokens.items()} if variants else fix(base.tokens)
    if tokens == base.tokens:
        raise RuntimeError(f"this Pygments release spells {base.__name__}'s rule differently")
    extra: dict[str, Any] = {"tokens": tokens, "aliases": [], "filenames": [], "_all_tokens": {}}
    return type(f"Fleet{base.__name__}", (base,), extra)


STOCK_HEREDOC = r"<<-?\s*(\'?)\\?(\w+)[\w\W]+?\2"
PATCHED = {
    "bash": patched(BashLexer, STOCK_HEREDOC, (HEREDOC, String.Heredoc)),
    "bash-heredoc-as-code": patched(BashLexer, STOCK_HEREDOC, None),
    "csharp": patched(
        CSharpLexer, r"^(\s*)(\[.*?\])", (ATTRIBUTE, bygroups(Whitespace, Name.Attribute))
    ),
}


@functools.cache
def lexer_for(name: str) -> Lexer:
    if name in PATCHED:
        return PATCHED[name](stripnl=False, ensurenl=False)
    return get_lexer_by_name(name, stripnl=False, ensurenl=False)


class Starts:
    """Line and column of a character offset into one text, and the text of a line."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def at(self, offset: int) -> tuple[int, int]:
        row = bisect_right(self.starts, offset) - 1
        return row + 1, offset - self.starts[row]

    def line(self, number: int) -> str:
        end = self.starts[number] - 1 if number < len(self.starts) else len(self.text)
        return self.text[self.starts[number - 1] : end]


def comment_tokens(text: str, lexer: str) -> list[tuple[int, str]]:
    """Each comment as (offset, text), the pieces PowerShell's lexer splits a comment into joined."""
    out: list[tuple[int, str]] = []
    state = ""
    for offset, kind, value in lexer_for(lexer).get_tokens_unprocessed(text):
        block = kind in Comment.Multiline
        joined = out and out[-1][0] + len(out[-1][1]) == offset
        if joined and ((block and state == "block") or (kind in String.Doc and state)):
            out[-1] = (out[-1][0], out[-1][1] + value)
        elif kind in Comment and not (kind in Comment.Preproc or kind in Comment.Hashbang):
            stripped = value.lstrip(" \t")
            out.append((offset + len(value) - len(stripped), stripped))
            state = "block" if block else "line"
        else:
            state = ""
    return out


def code_follows(text: str, found: list[tuple[int, str]], k: int) -> bool:
    """Whether code follows comment `k` of `found` on the line it ends on, a later comment aside."""
    at = found[k][0] + len(found[k][1])
    stop = len(text) if (eol := text.find("\n", at)) < 0 else eol
    for offset, value in found[k + 1 :]:
        if offset >= stop or text[at:offset].strip():
            break
        at = offset + len(value)
        if "\n" in value:
            return False
    return bool(text[at:stop].strip())


def comment_lines(
    text: str, lexer: str, spec: dict[str, Any], shift: int = 0
) -> list[tuple[Line, bool]]:
    """Every comment line in `text`, and whether it is a documentation comment.

    A comment opening on no marker the language declares belongs to a language embedded in it, such
    as a script in HTML. Such a comment is left out. `shift` is the line `text` starts after.
    """
    starts = Starts(text)
    out: list[tuple[Line, bool]] = []
    blocks = sorted(spec.get("block", ()), key=lambda b: -len(b[0]))
    markers = sorted(spec.get("line", ()), key=len, reverse=True)
    found = comment_tokens(text, lexer)
    for k, (offset, value) in enumerate(found):
        value = value.rstrip("\r\n")
        number, col = starts.at(offset)
        leading = not starts.line(number)[:col].strip()
        doc = value.startswith(tuple(spec.get("doc_markers", ())))
        help_form = value.startswith(tuple(spec.get("help_markers", ())))
        block = next((b for b in blocks if value.startswith(b[0])), None)
        marker = next((m for m in markers if value.startswith(m)), None)
        if block is None and marker is None:
            continue
        if block is None and marker is not None:
            body = value[len(marker) :]
            body = body.lstrip(marker) if spec.get("repeat_marker") else body
            out.append((Line(number + shift, body.strip(), leading, value, help_form), doc))
            continue
        opener, closer, *cont = block or ("", "")
        parts = value[len(opener) :].removesuffix(closer).split("\n")
        trailed = code_follows(text, found, k)
        for i, part in enumerate(parts):
            body = part.strip()
            if i and cont and body.startswith(cont[0]) and body[1:2].isspace():
                body = body[1:].strip()
            elif i and cont and body == cont[0]:
                body = ""
            row = starts.line(number + i)
            raw = row[min(col, len(row) - len(row.lstrip())) :] if i else value.split("\n")[0]
            last = trailed and i == len(parts) - 1
            out.append(
                (Line(number + i + shift, body, leading or i > 0, raw, help_form, last), doc)
            )
    return out


def nested_spans(text: str, rules: list[dict[str, str]]) -> list[tuple[int, int, str, int]]:
    """The YAML scalars under a key or a tag the data names, as (start, end, lexer, key) offsets."""
    keys = {r["key"]: r["lexer"] for r in rules if "key" in r}
    tags = {r["tag"]: r["lexer"] for r in rules if "tag" in r}
    tokens = list(lexer_for("yaml").get_tokens_unprocessed(text))
    out: list[tuple[int, int, str, int]] = []
    pending: str | None = None
    i = key = 0
    while i < len(tokens):
        offset, kind, value = tokens[i]
        i += 1
        if kind in Name.Tag and value in keys:
            pending, key = keys[value], offset
        elif kind in Keyword.Type and value in tags:
            pending, key = tags[value], offset
        elif pending and kind in Punctuation.Indicator and value in "|>":
            # The rest of the header line, a comment after the indicator included, opens nothing.
            while i < len(tokens) and "\n" not in tokens[i][2]:
                i += 1
            j = i
            while j < len(tokens) and (
                tokens[j][1] in Text.Whitespace or tokens[j][1] in Name.Constant
            ):
                j += 1
            if j > i:
                # The span opens after the header's own line break, so its lines map one to one.
                start = text.index("\n", offset) + 1
                out.append(
                    (start, max(start, tokens[j - 1][0] + len(tokens[j - 1][2])), pending, key)
                )
            i, pending = j, None
        elif pending and kind in String and value in "'\"":
            j = i
            while j < len(tokens) and not (tokens[j][1] in String and tokens[j][2] == value):
                j += 1
            if j < len(tokens):
                out.append((offset + 1, tokens[j][0], pending, key))
            i, pending = j + 1, None
        elif not (kind in Text.Whitespace or kind in Punctuation or kind in Keyword.Type):
            pending = None
    return out


def step_shell(starts: Starts, key: int) -> str:
    """The program `shell:` names beside the YAML key at offset `key`, or "" where none is named."""
    (row, col), last = starts.at(key), len(starts.starts)
    inside = lambda n: not starts.line(n)[:col].strip() or starts.line(n).lstrip().startswith("#")
    top, end = row, row + 1
    while top > 1 and inside(top):
        top -= 1
    top += not inside(top) and starts.line(top)[:col].strip() != "-"
    while end <= last and inside(end):
        end += 1
    sibling = re.compile(rf"[ \t-]{{{col}}}shell:[ \t]*['\"]?(?:\S*/)?([\w-]+)")
    return next((m[1] for n in range(top, end) if (m := sibling.match(starts.line(n)))), "")


HEREDOC_LABEL = re.compile(r"<<-?[ \t]*(['\"]?)\\?[\w-]+\1")


def runs_body(starts: Starts, row: int, programs: frozenset[str]) -> bool:
    """Whether the command opening a heredoc on `row` names one of `programs`, which runs the body.

    The command is the opener line without its heredoc labels, after any lines a trailing backslash
    continues onto it. Any other command reads the body as the data it writes.
    """
    top = row
    while top > 1 and starts.line(top - 1).rstrip().endswith("\\"):
        top -= 1
    command = HEREDOC_LABEL.sub(" ", " ".join(starts.line(n) for n in range(top, row + 1)))
    words = re.split(r"[\s|;&()`\"'{}<>=\\]+", command)
    return any(w.rsplit("/", 1)[-1].rstrip("0123456789.") in programs for w in words)


def split_heredocs(text: str, programs: frozenset[str]) -> list[tuple[int, str]]:
    """A script as its code with heredoc bodies blanked, then each body, as (line offset, text).

    A body's prose can hold an apostrophe, which the shell lexer reads as a string running on to the
    next one anywhere after it. The bodies come from the lexer that knows shell state, run on the
    script without its YAML indent. Each is lexed alone so that string ends with the body. Only a
    body a program in `programs` runs is returned, since any other is data rather than code.
    """
    rows = text.splitlines(keepends=True)
    indent = min((len(r) - len(r.lstrip()) for r in rows if r.strip()), default=0)
    plain = "".join(r[indent:] if r.strip() else r for r in rows)
    starts = Starts(plain)
    spans = []
    for offset, kind, value in lexer_for("bash").get_tokens_unprocessed(plain):
        if kind in String.Heredoc:
            # The opener's own line stays code, so a body is the lines after it.
            first, end = starts.at(offset)[0], starts.at(offset + len(value) - 1)[0]
            mid = starts.at(offset + max(next(re.finditer(HEREDOC, value)).end(8), 1) - 1)[0]
            run = runs_body(starts, first, programs)
            pieces = [(first, mid), (mid, end)] if first < mid < end else [(first, end)]
            spans += [(a, b, run) for a, b in pieces]
    code = list(rows)
    for first, end, _run in spans:
        code[first:end] = ["\n"] * (end - first)
    return [(0, "".join(code)), *((a, "".join(rows[a:b])) for a, b, run in spans if run)]


def nested_comments(text: str, spec: dict[str, Any]) -> list[tuple[Line, bool]]:
    """Comments in the scalars a YAML file hands to another lexer, numbered by the file's lines."""
    starts = Starts(text)
    out: list[tuple[Line, bool]] = []
    for start, end, lexer, key in nested_spans(text, spec.get("nested", [])):
        number, col = starts.at(start)
        inner = language_of(lexer) or {}
        # The column pad keeps a one-line scalar's comment trailing, and C++ ends a comment on a break.
        body = text[start:end]
        if lexer == "bash-heredoc-as-code" and step_shell(starts, key) in ("", "bash", "sh"):
            # Each body starts on a line of its own, so only the code piece takes the column pad.
            for skip, piece in split_heredocs(body, frozenset(spec.get("heredoc_programs", ()))):
                pad = "x" * col if skip == 0 else ""
                out += comment_lines(pad + piece + "\n", lexer, inner, number - 1 + skip)
            continue
        out += comment_lines("x" * col + body + "\n", lexer, inner, number - 1)
    return out


def python_docstrings(text: str) -> list[Unit]:
    """Each docstring, the first statement of a module, class, or function, as its source lines."""
    starts = Starts(text)
    units: list[Unit] = []
    expect, header, depth = True, False, 0
    tokens = list(lexer_for("python").get_tokens_unprocessed(text))
    i = 0
    while i < len(tokens):
        offset, kind, value = tokens[i]
        i += 1
        if kind in Text or kind in Comment:
            continue
        if expect and kind in String:
            j = i
            while j < len(tokens) and tokens[j][1] in String:
                j += 1
            units.append(docstring_unit(starts, offset, tokens[j - 1][0] + len(tokens[j - 1][2])))
            i = j
        elif kind in Keyword and value in ("def", "class"):
            header = True
        elif kind in Punctuation and value in "([{)]}":
            depth += 1 if value in "([{" else -1
        elif header and depth == 0 and kind in Punctuation and value == ":":
            header, expect = False, True
            continue
        expect = False
    return units


def docstring_unit(starts: Starts, start: int, end: int) -> Unit:
    """One docstring's lines, and the column its text is measured from."""
    first, col = starts.at(start)
    last, end_col = starts.at(end)
    lines = tuple(
        Line(n, starts.line(n)[(col if n == first else 0) : (end_col if n == last else None)])
        for n in range(first, last + 1)
    )
    before = starts.line(first)[:col].expandtabs(4)
    column = len(before) if not before.strip() else leading_indent(before) + INDENT_BLOCK
    return Unit("docstring", lines, column)


def blocks(kind: str, found: list[Line]) -> list[Unit]:
    """Comment lines grouped into runs on the same or the next line, each run one unit."""
    out: list[list[Line]] = []
    for line in found:
        if out and line.number - out[-1][-1].number in (0, 1):
            out[-1].append(line)
        else:
            out.append([line])
    return [Unit(kind, tuple(group)) for group in out]


def line_units(kind: str, lines: list[str], skip: frozenset[int]) -> list[Unit]:
    """The lines outside `skip`, one unit per run between skipped lines."""
    runs: list[list[Line]] = [[]]
    for n, text in enumerate(lines, 1):
        if n in skip:
            runs.append([])
        else:
            runs[-1].append(Line(n, text))
    return [Unit(kind, tuple(run)) for run in runs if run]


def fenced_lines(lines: list[str]) -> frozenset[int]:
    """Lines in a fenced example inside a code file's comment or string, quoted rather than stated."""
    out: set[int] = set()
    inside = False
    for n, line in enumerate(lines, 1):
        fence = bool(CODE_FENCE.match(line))
        inside ^= fence
        if fence or inside:
            out.add(n)
    return frozenset(out)


YAML_LINE = re.compile(r"^(?:\s*$|\s+\S|\s*-(?:\s|$)|#|[^\s:#][^:]*:(?:\s.*)?$)")


def front_matter_end(lines: list[str]) -> int:
    """The index after a leading `---` block closed by `---` or `...`, or 0 when there is none."""
    if not lines or lines[0] != "---":
        return 0
    for j in range(1, len(lines)):
        if lines[j] in ("---", "..."):
            return j + 1
        if not YAML_LINE.match(lines[j]):
            return 0
    return 0


MARKDOWN = MarkdownIt("commonmark").enable("table")
HTML = BASE["languages"]["markup"]
TAG = re.compile(r"<[^<>]*>")
BLOCK_PARENTS = (["paragraph_open"], ["th_open"], ["td_open"])


def extract_markdown(lines: list[str]) -> Extracted:
    """Markdown units from markdown-it-py's block line maps, front matter lines read alone."""
    matter = front_matter_end(lines)
    code: set[int] = set()
    paragraphs = [
        Unit("paragraph", (Line(n, line.strip()),))
        for n, line in enumerate(lines[:matter], 1)
        if line.strip() and not line.strip().startswith("#")
    ]
    markup: list[Line] = []
    parents: list[str] = []
    for tok in MARKDOWN.parse("\n".join([""] * matter + lines[matter:])):
        if tok.nesting:
            parents = [*parents, tok.type] if tok.nesting == 1 else parents[:-1]
        if not tok.map:
            continue
        first, last = tok.map[0] + 1, tok.map[1]
        if tok.type in ("fence", "code_block"):
            code.update(range(first, last + 1))
        elif tok.type == "html_block":
            found = [line for line, _ in comment_lines(tok.content, "html", HTML, first - 1)]
            markup += found
            inside = {line.number for line in found}
            for n in range(first, last + 1):
                span = TAG.sub(lambda m: " " * len(m.group()), lines[n - 1]).strip()
                if span and n not in inside:
                    paragraphs.append(Unit("paragraph", (Line(n, span),)))
        elif tok.type == "inline":
            markup += inline_comments(tok, lines)
            if parents[-1:] in BLOCK_PARENTS and "blockquote_open" not in parents:
                spans = enumerate(tok.content.split("\n"))
                paragraphs.append(
                    Unit("paragraph", tuple(Line(first + i, s.strip()) for i, s in spans))
                )
    skip = frozenset(code)
    units = [
        Unit("source", tuple(Line(n, t) for n, t in enumerate(lines, 1))),
        *line_units("prose", lines, skip),
        *paragraphs,
        *blocks("markup", sorted(markup, key=lambda m: m.number)),
    ]
    return units, skip, False


def inline_comments(tok: Token, lines: list[str]) -> list[Line]:
    """The HTML comments inside one inline token, numbered by the source line each opens on."""
    out: list[Line] = []
    first, last = (tok.map or [0, 1])[0], (tok.map or [0, 1])[1]
    number, col = first + 1, 0
    # A code span can hold a comment's text, so it is blanked, lengths kept, before the search.
    blank = lambda m: re.sub(r"[^\n]", " ", m.group()) if m[1] else m.group()
    seen = re.sub(MARKDOWN_SPAN, blank, "\n".join(lines[first:last])).split("\n")
    for child in tok.children or ():
        if child.type == "html_inline" and child.content.startswith("<!--"):
            # The source line is found by the comment's own text, searched on from the last one.
            head = child.content.split("\n")[0]
            for rows, word in ((seen, head), (lines[first:last], head)):
                at, n = next(
                    (
                        (r.find(word, col if i == number else 0), i)
                        for i in range(number, last + 1)
                        if (r := rows[i - first - 1]).find(word, col if i == number else 0) >= 0
                    ),
                    (-1, 0),
                )
                if at >= 0:
                    number, col = n, at + len(head)
                    break
            before = lines[number - 1][: max(col - len(head), 0)]
            for line, _ in comment_lines(child.content, "html", HTML, number - 1):
                out.append(line._replace(leading=line.number > number or not before.strip()))
    return out


def extract(path: Path, text: str, lex: bool = True) -> Extracted:
    """The units the rules read in this file, and whether it was read as plain text.

    Without `lex`, a code file's comments and docstrings are left unread, no rule asking for them.
    """
    lines = [line.rstrip("\r") for line in text.split("\n")]
    lexer = lexer_name(path, lines[0])
    if lexer == "markdown":
        return extract_markdown(lines)
    fenced = fenced_lines(lines)
    units = [
        Unit("source", tuple(Line(n, t) for n, t in enumerate(lines, 1))),
        *line_units("code", lines, fenced),
    ]
    spec = language_of(lexer)
    if spec is None or lexer is None or not lex:
        return units, fenced, lex and path.suffix.lower() not in BASE["files"]["plain"]
    clean = text.replace("\r\n", "\n")
    found = comment_lines(clean, lexer, spec)
    if spec.get("nested"):
        found = sorted(found + nested_comments(clean, spec), key=lambda f: f[0].number)
    units += blocks("comment", [line for line, doc in found if not doc])
    units += blocks("doc", [line for line, doc in found if doc])
    if spec.get("docstrings"):
        units += python_docstrings(clean)
    return units, fenced, False


# Normalize and evaluate: the rules.


class Context(NamedTuple):
    """What a rule knows about the file a unit came from, and the lines a diff scopes it to."""

    path: Path
    root: Path | None
    fenced: frozenset[int]
    language: dict[str, Any]
    config: Config
    allowed: set[int] | None = None


# The one sentence boundary every reader shares, a terminator that no initial or abbreviation ends.
# The last dot of an ellipsis ends nothing either.
TERMINATOR = r"(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)(?:(?<!\.)\.|[!?])"
# In Markdown a sentence may close inside emphasis, a code span, a quotation, or a bracket.
SENTENCE_BREAK = re.compile(TERMINATOR + r"[*_`\"')\]]*\s+")
# On a comment line the second sentence opens on a letter, in either case.
RUN_ON = re.compile(TERMINATOR + r"\s+(?=[A-Za-z])")
# Text that closes as a sentence does, or, with the colon, as a lead-in to what follows does.
SNIPPET_END = re.compile(r"[.!?][\"')\]]?\s*$")
SENT_END = re.compile(r"[.!?:][\"')\]]?\s*$")
# A spaced hyphen, the em-dash-style clause break and the paired aside alike.
# A compound word carries no spaces, and a range is digit-bounded.
DASH = re.compile(r"(?<=[^\s\d])\s+-\s+(?=[^\s\d])")
QUOTE_PREFIX = re.compile(r"^\s*(?:>\s*)+")
LIST_MARKER_TOKEN = r"(?:[-*+]|([0-9]+)[.)])"
STRONG_LABEL_TOKEN = (
    r"(?P<outer>[*_]?)(?:(?P<star>\*\*)(?P<si>_?)|__(?P<ui>\*?))"
    r"(?(star)[^*\n]|[^_\n])+?(?P<inner_colon>:\s*)?"
    r"(?(star)(?P=si)\*\*|(?P=ui)__)(?P=outer)"
)
# `- **Label** - explanation` is a definition separator, structurally a colon, so its dash is skipped.
LABEL_DASH = re.compile(rf"^\s*{LIST_MARKER_TOKEN}\s+{STRONG_LABEL_TOKEN}[.:]?\s+-\s+")
# A bullet's `**Label**:` or `**Label:**` introduces the bullet rather than a list.
LABEL_COLON = re.compile(rf"^\s*{LIST_MARKER_TOKEN}\s+{STRONG_LABEL_TOKEN}(?(inner_colon)|\s*:)")
# A lone semicolon separates a list only between labeled items, as in `Inputs: a, b; outputs: c`.
LABEL_WORD = r"[\w`]+(?:[./-][\w`]+)*"
LABELED_ITEM = re.compile(rf"^\s*[*_]*{LABEL_WORD}(?:\s+{LABEL_WORD}){{0,2}}[*_]*:[*_]*(?:\s|$)")
LIST_MARKER = re.compile(rf"^\s*{LIST_MARKER_TOKEN}\s")
CELL_BREAK = re.compile(r"(?<!\\)\|")
TABLE_ROW = re.compile(r"^\s*(?:>\s*)*\|")
# A word-joining character before the repeat keeps "either/or or" from reading as a doubled word.
DUPWORD = re.compile(r"(?<![\w/-])(\w+)\s+\1\b", re.IGNORECASE)
# A step marker is a label on the sentence that follows, so its dot terminates nothing.
ENUM_PREFIX = re.compile(r"^\d+[.)]\s+")
# A body that is only a URI is a reference, and a lone key is disabled configuration.
BARE_URI = re.compile(r"^(?:<(?:https?|ftp)://[^>\s]+>|(?:https?|ftp)://[^>\s]+)$", re.IGNORECASE)
KEY_ONLY = re.compile(r"^\S+:$")
# One key with one value is a line a reader pastes, unless it closes as a sentence does.
SNIPPET = re.compile(r"^\S+:\s+\S+$")
# A key, a separator, a space, and a value read as configuration only in a run of two or more.
# One such line alone is as often a marker comment like `note: obsolete`.
CONFIG_LINE = re.compile(r"^\S+\s*[:=]\s+\S")
CONFIG_RUN_MIN = 2
# A label opening a definition names the thing defined, so its lowercase is no sentence opening.
COMMENT_LABEL = re.compile(r"^[A-Za-z_][\w.-]*\s+-\s+")
INDENT_BLOCK = 4


def strip_inline_code(s: str) -> str:
    return re.sub(r"`[^`]*`", "``", s)


def strip_quoted(s: str) -> str:
    """Blank a Markdown quotation, keeping the terminator US English closes a sentence inside."""
    return re.sub(r'"([^"\n]*?)([.!?]?)"', r'""\2', s)


def starts_lowercase(text: str) -> bool:
    """Whether the text opens with a lowercase letter, a tier-4 marker being no letter."""
    return text[:1].islower() and text[0] not in TIER4


def leading_indent(line: str) -> int:
    """The columns of leading space on a source line, tabs read as four."""
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def strip_list_marker(text: str) -> str:
    return LIST_MARKER.sub("", text, count=1).strip()


def row_cells(text: str) -> list[str]:
    """A table row opening with a pipe split into its cells, and any other line left whole.

    A cell ends at a pipe no backslash escapes, inside a code span too, which is how markdown-it-py
    splits a row. So no code span or quotation read from a cell reaches into the next one.
    """
    return CELL_BREAK.split(text) if TABLE_ROW.match(text) else [text]


def by_cell(text: str, blank: Callable[[str], str]) -> str:
    """Blank a line a cell at a time, keeping every pipe that ends a cell where it was.

    An unpaired quote mark in one cell would otherwise hide every cell up to the next quote.
    """
    return "|".join(blank(cell) for cell in row_cells(text))


def prose_lines(unit: Unit) -> Iterable[tuple[int, str, str]]:
    """Each line's number, its text without code spans, and that text without quotations either."""
    for line in unit.lines:
        txt = by_cell(line.text, strip_inline_code)
        prose = by_cell(txt, strip_quoted)
        yield line.number, txt, QUOTE_PREFIX.sub(lambda m: " " * len(m.group()), prose, count=1)


def in_numeric_context(line: str, pos: int) -> bool:
    """Whether a tier-2 character is flanked by a digit, a unit, or an operator, past any spaces."""
    flanks = (line[:pos].rstrip()[-1:], line[pos + 1 :].lstrip()[:1])
    return any(c and (c in "0123456789" or c in TIER3 or c in TIER2) for c in flanks)


COMBINING_DIACRITICS = frozenset(map(chr, range(0x300, 0x370))) - {chr(0x34F)}


def charset_findings(lineno: int, line: str) -> list[Finding]:
    """Each non-ASCII character judged by its tier, a Latin letter and its diacritics passed."""
    out: list[Finding] = []
    carrier = marker = False
    for pos, ch in enumerate(line):
        after_marker, marker = marker, ch in TIER4
        if ch.isascii():
            carrier = ch.isalpha()
            continue
        if marker or (after_marker and ch == VARIATION_SELECTOR_16):
            carrier = False
            continue
        name = unicodedata.name(ch, f"U+{ord(ch):04X}")
        if (
            unicodedata.category(ch).startswith("L")
            and name.startswith("LATIN ")
            and not unicodedata.decomposition(ch).startswith("<")
        ):
            carrier = True
            continue
        if carrier and ch in COMBINING_DIACRITICS:
            continue
        carrier = False
        code = f"{ord(ch):04X}"
        if ch in TIER1:
            fix = TIER1[ch]
            hint = "restructure the sentence" if fix == "restructure" else f"use '{fix}'"
            out.append(finding(lineno, "charset", name=name, code=code, hint=hint))
        elif ch in TIER2 and not in_numeric_context(line, pos):
            out.append(
                finding(lineno, "charset", "charset-tier2", name=name, code=code, fix=TIER2[ch])
            )
        elif ch not in TIER2 and ch not in TIER3:
            out.append(finding(lineno, "charset-unknown", name=name, code=code))
    return out


def rule_charset(unit: Unit, ctx: Context) -> list[Finding]:
    return [f for line in unit.lines for f in charset_findings(line.number, line.text)]


def rule_semicolon(unit: Unit, ctx: Context) -> list[Finding]:
    """A Markdown semicolon, unless its sentence is a list carrying a comma that needs one.

    A table row opening with a pipe is read a cell at a time, split where `row_cells` splits it.
    `prose_lines` blanks each cell on its own and leaves every pipe ending one where it was.
    """
    out: list[Finding] = []
    for n, _txt, line in prose_lines(unit):
        pieces = (
            s for cell in row_cells(line) for s in SENTENCE_BREAK.split(LABEL_COLON.sub("", cell))
        )
        for sentence in (s for s in pieces if s.strip()):
            head, _, tail = sentence.partition(";")
            listish = sentence.count(";") > 1 or (
                LABELED_ITEM.match(LIST_MARKER.sub("", head, count=1)) is not None
                and LABELED_ITEM.match(tail) is not None
            )
            if not (listish and "," in sentence):
                out += [finding(n, "semicolon")] * sentence.count(";")
    return out


def rule_dash(unit: Unit, ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    for n, _txt, line in prose_lines(unit):
        skip = LABEL_DASH.match(line)
        out += [
            finding(n, "dash") for m in DASH.finditer(line) if not (skip and m.start() < skip.end())
        ]
    return out


def unit_texts(unit: Unit, ctx: Context) -> Iterable[tuple[int, str]]:
    """Each unfenced line's prose: Markdown without code spans or quotations, a comment as written."""
    for line in unit.lines:
        if line.number in ctx.fenced:
            continue
        if unit.kind != "prose":
            yield line.number, strip_inline_code(line.text)
            continue
        text = by_cell(by_cell(line.text, strip_inline_code), strip_quoted)
        yield line.number, by_cell(text, strip_inline_code)


def rule_dupword(unit: Unit, ctx: Context) -> list[Finding]:
    """A duplicated word, each comment judged on its own so a neighbor's last word is no repeat."""
    allow = ctx.config.exempt["dupword_allow"]
    return [
        finding(n, "dupword", word=m.group(1))
        for n, text in unit_texts(unit, ctx)
        for m in DUPWORD.finditer(text)
        if m.group(0).lower() not in allow
    ]


def rule_spelling(unit: Unit, ctx: Context) -> list[Finding]:
    joined: dict[int, list[str]] = {}
    for n, text in unit_texts(unit, ctx):
        joined.setdefault(n, []).append(text)
    out: list[Finding] = []
    for n, texts in joined.items():
        for m in ctx.config.british_re.finditer(" ".join(texts)):
            found = m.group(0)
            us = ctx.config.british[found.lower()]
            us = us.upper() if found.isupper() else us.capitalize() if found[0].isupper() else us
            out.append(finding(n, "spelling", found=found, us=us))
    return out


def rule_sentence_split(unit: Unit, ctx: Context) -> list[Finding]:
    """A Markdown line ending mid-sentence that the next prose line continues in lowercase."""
    out: list[Finding] = []
    prev, prev_no = "", 0
    for n, txt, _prose in prose_lines(unit):
        stripped = txt.strip()
        is_prose = bool(stripped) and not stripped.startswith(("|", ">", "#"))
        is_prose = is_prose and not re.match(r"^\s*\[[^\]]+\]:", stripped)
        p = prev.strip()
        ended = not p or SENT_END.search(p) or p.endswith((":", "-", "|"))
        if is_prose and not ended and starts_lowercase(stripped):
            out.append(finding(prev_no, "sentence-split"))
        prev, prev_no = (txt if is_prose else ""), n
    return out


HOME_PATH = re.compile(
    r"(?:/home/|/Users/|[A-Za-z]:\\(?i:users)\\)(?P<user>[A-Za-z][A-Za-z0-9._-]*)"
)


def rule_home_path(unit: Unit, ctx: Context) -> list[Finding]:
    """An absolute home path naming a real account, on any line, a fenced transcript included."""
    exempt = ctx.config.exempt
    if ctx.root is not None and ctx.path.resolve() in {
        (ctx.root / p).resolve() for p in exempt["home_path_exempt"]
    }:
        return []
    return [
        finding(line.number, "home-path", path=m.group(0))
        for line in unit.lines
        for m in HOME_PATH.finditer(line.text)
        if m.group("user").lower() not in exempt["service_accounts"]
    ]


# A backtick span, an inline link target, and a reference definition each assert a path.
INLINE_SPAN = re.compile(r"`([^`\n]+)`")
LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")
REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s+(\S+)")
# A placeholder, a glob, an expansion, or a scheme, the colon covering URLs and drive letters.
PATH_FOREIGN = frozenset("<>{}$*?\"'\\:!|,;")


def path_candidate(token: str, in_span: bool = True) -> str | None:
    """The relative path a token asserts, a backtick span only where it is shaped like a file."""
    token = token.split("#", 1)[0]
    if not token or any(c.isspace() for c in token) or PATH_FOREIGN & set(token):
        return None
    if token.startswith(("/", "~", "-", "#")):
        return None
    last = token.rsplit("/", 1)[-1]
    if in_span and ("/" not in token or "." not in last or not last.strip(".")):
        return None
    return token.removeprefix("./")


@functools.cache
def carried_paths(root: str) -> frozenset[str]:
    """Paths the manifest declares as carried, which docs name as fleet layout."""
    try:
        data = json.loads((Path(root) / "spec" / "files.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    entries = data.get("baseline", [])
    return frozenset(
        e["path"] for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str)
    )


@functools.cache
def once_tracked(root: str, rel_path: str) -> bool:
    """Whether git at `root` ever recorded `rel_path`, the deletion signature this rule keys on."""
    return bool((git_text("-C", root, "log", "-1", "--format=%H", "--", rel_path) or "").strip())


def rule_dead_path(unit: Unit, ctx: Context) -> list[Finding]:
    """A Markdown path git once tracked and the tree no longer holds, unless it lives elsewhere."""
    root = ctx.root or (Path(found) if (found := repo_root(ctx.path)) else None)
    if root is None:
        return []
    base = ctx.path.resolve().parent
    exempt = carried_paths(str(root)) | ctx.config.exempt["hub_hosted"]
    out: list[Finding] = []
    for line in unit.lines:
        m = REF_DEF.match(line.text)
        tokens = (
            [(m.group(1), False)]
            if m
            else [(s.group(1), True) for c in row_cells(line.text) for s in INLINE_SPAN.finditer(c)]
        )
        if not m:
            tokens += [
                (t.group(1), False)
                for t in LINK_TARGET.finditer(by_cell(line.text, strip_inline_code))
            ]
        for token, in_span in tokens:
            rel_path = path_candidate(token, in_span)
            if rel_path is None or (root / rel_path).exists() or (base / rel_path).exists():
                continue
            for anchor in {root, base}:
                try:
                    key = (anchor / rel_path).resolve().relative_to(root.resolve()).as_posix()
                except ValueError:
                    continue
                if key not in exempt and once_tracked(str(root), key):
                    out.append(finding(line.number, "dead-path", token=token))
                    break
    return out


def is_instruction_text(path: Path, root: Path | None) -> bool:
    """Whether this Markdown file is instruction text, a skill matched by its root."""
    if path.suffix.lower() not in BASE["files"]["markdown"]:
        return False
    key = repo_key(path, root) if root is not None else rel(path)
    if key in INSTRUCTION_DOCS or key.startswith(tuple(BASE["skill_roots"])):
        return True
    plugin, skills = BASE["plugin_skills"]
    return key.startswith(plugin) and skills in key[len(plugin) :]


# A bare reference, two to five digits: one is an ordinal, and six is a hex color.
ISSUE_REF = re.compile(r"(?<!#)#([0-9]{2,5})(?!\w)")
# A URI, whose fragment is a number after a `#` and is not a reference to anything here.
URI_SPAN = re.compile(r"[a-z][a-z0-9+.-]*://[^\s<>\])]+", re.IGNORECASE)
# The link spelling, read in instruction text only, on the one forge the rule text names.
FORGE_REF = re.compile(
    r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/([0-9]+)", re.IGNORECASE
)


def without_lookalikes(text: str, markdown: bool) -> str:
    """The span with a URI, and in Markdown a link destination, blanked to the same length."""
    spans = [(m.start(), m.end()) for m in URI_SPAN.finditer(text)]
    if markdown:
        spans += [(m.start(1), m.end(1)) for p in (LINK_TARGET, REF_DEF) for m in p.finditer(text)]
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def rule_issue_ref(unit: Unit, ctx: Context) -> list[Finding]:
    """An issue or pull request reference on a surface the ban reaches.

    Instruction text is read outside its fences, for the bare form and the forge link. Elsewhere
    a leading comment is read with and without its marker, and a docstring as source. A string
    literal is not, being fixture data as often as not. A line reports each number once.
    """
    spans: dict[int, list[str]] = {}
    links: dict[int, list[str]] = {}
    if unit.kind == "prose" and not is_instruction_text(ctx.path, ctx.root):
        return []
    for line in unit.lines:
        if unit.kind == "prose":
            spans.setdefault(line.number, []).append(without_lookalikes(line.text, True))
            links.setdefault(line.number, []).append(line.text)
        elif unit.kind == "docstring" or line.leading:
            texts = [line.text, line.raw] if line.raw else [line.text]
            spans.setdefault(line.number, []).extend(without_lookalikes(t, False) for t in texts)
    out: list[Finding] = []
    seen: set[tuple[int, str]] = set()
    for n in sorted(set(spans) | set(links)):
        found = [(ISSUE_REF, t) for t in spans.get(n, ())] + [
            (FORGE_REF, t) for t in links.get(n, ())
        ]
        for m in (m for pattern, text in found for m in pattern.finditer(text)):
            if (n, m.group(1)) not in seen:
                seen.add((n, m.group(1)))
                out.append(finding(n, "issue-ref", ref=m.group(0)))
    return out


def without_trailing_directive(ctx: Context, body: str) -> str:
    """The body up to the first directive this language's checker reads after prose on a line."""
    for pattern in ctx.language.get("trailing_directives", ()):
        m = re.search(pattern, body)
        body = body[: m.start()].rstrip() if m else body
    return body


def is_tool_directive(body: str, config: Config) -> bool:
    """Whether a body opens on a directive's name and does not close as a sentence does."""
    return bool(config.tool_directive.match(body)) and not SENT_END.search(body)


def is_config_line(line: Line) -> bool:
    return line.leading and bool(CONFIG_LINE.match(line.text)) and not SNIPPET_END.search(line.text)


def config_run_lines(lines: list[Line]) -> set[int]:
    """The lines of each run of `CONFIG_RUN_MIN` or more adjacent leading configuration comments."""
    out: set[int] = set()
    run: list[int] = []
    for line in [*lines, None]:
        if line is None or not is_config_line(line) or (run and line.number != run[-1] + 1):
            out.update(run if len(run) >= CONFIG_RUN_MIN else ())
            run = []
        if line is not None and is_config_line(line):
            run.append(line.number)
    return out


def prose_comments(unit: Unit, ctx: Context) -> tuple[list[Line], list[Line], set[int]]:
    """The unfenced lines cut at a trailing directive, the lines as written, and the config runs."""
    kept = [line for line in unit.lines if line.number not in ctx.fenced]
    trimmed = [line._replace(text=without_trailing_directive(ctx, line.text)) for line in kept]
    return trimmed, kept, config_run_lines(trimmed)


def is_non_prose_comment(line: Line, configured: set[int], config: Config) -> bool:
    body = line.text
    return bool(
        not body
        or line.number in configured
        or config.not_prose.search(body)
        or BARE_URI.match(body.strip())
        or KEY_ONLY.match(body)
        or (SNIPPET.match(body) and not SNIPPET_END.search(body))
    )


def rule_comment_wrap(unit: Unit, ctx: Context) -> list[Finding]:
    """One sentence per comment line, read as `comment-wrap` and `comment-case`.

    A sentence wrapped into the next line and two on one line are wraps, and a lowercase opening
    continuing nothing is a case finding. An unpunctuated Markdown HTML comment is a marker.
    """
    comments, kept, configured = prose_comments(unit, ctx)
    out: list[Finding] = []
    prev_body = prev_whole = ""
    prev_no = 0
    for line, whole in zip(comments, kept, strict=True):
        if is_non_prose_comment(line, configured, ctx.config) or (
            unit.kind == "markup" and not SENT_END.search(line.text)
        ):
            prev_body = ""
            continue
        n, body = line.number, ENUM_PREFIX.sub("", line.text)
        if RUN_ON.search(strip_inline_code(body)):
            out.append(finding(n, "comment-wrap", "comment-run-on"))
        directive = is_tool_directive(body, ctx.config)
        lower = line.leading and starts_lowercase(body)
        ended = SENT_END.search(prev_body) or SENT_END.search(prev_whole)
        if lower and n == prev_no + 1 and prev_body and not ended:
            out.append(finding(prev_no, "comment-wrap"))
        elif lower and not COMMENT_LABEL.match(body) and not directive:
            out.append(finding(n, "comment-case"))
        prev_body = "" if directive else body
        prev_whole, prev_no = whole.text, n
    return out


def is_comment_prose(body: str, config: Config) -> bool:
    """Whether a body is prose, a key-value snippet included, pricing one being this rule's job."""
    return bool(
        body
        and not config.not_prose.search(body)
        and not is_tool_directive(body, config)
        and not BARE_URI.match(body)
        and not KEY_ONLY.match(body)
    )


def rule_comment_added(unit: Unit, ctx: Context) -> list[Finding]:
    """Every prose comment line opening its own line, which the diff scope narrows to a change.

    A modified line counts as added, the label being the answer. A help block, a documentation
    comment, and a comment sharing its line with code, before or after it, are out of scope.
    """
    lines = {
        line.number
        for line in unit.lines
        if line.leading
        and not line.trailed
        and not line.help
        and is_comment_prose(without_trailing_directive(ctx, line.text), ctx.config)
    }
    return [finding(n, "comment-added") for n in sorted(lines)]


CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)", re.DOTALL)
NON_CODE = r"\\.|<[a-zA-Z][a-zA-Z0-9+.\-]{1,31}:[^<>\x00-\x20]*>"
MARKDOWN_SPAN = re.compile(rf"{NON_CODE}|{HTML_TAG_RE.pattern[1:]}|{CODE_SPAN.pattern}", re.DOTALL)


def mask_code_and_quotes(text: str, starts: list[int], lengths: list[int]) -> str:
    """The text with code spans and each line's quotations blanked, a closing terminator kept."""
    text = re.sub(CODE_SPAN, lambda m: "`" * len(m.group()), text)
    out = list(text)
    for start, length in zip(starts, lengths, strict=True):
        out[start : start + length] = re.sub(
            r'"([^"]*?)([.!?]?)"',
            lambda m: '"' * (len(m.group()) - len(m.group(2))) + m.group(2),
            text[start : start + length],
        )
    return "".join(out)


def over_cap(block: list[tuple[int, str]], allowed: set[int] | None) -> list[Finding]:
    """Over-cap sentences in one block, reported on the first line, or the first changed line."""
    if not block:
        return []
    starts = []
    raw = ""
    for _, span in block:
        raw += " " if raw else ""
        starts.append(len(raw))
        raw += span
    numbers = [n for n, _ in block]
    text = mask_code_and_quotes(raw, starts, [len(span) for _, span in block])
    out: list[Finding] = []
    pos = 0
    for m in [*SENTENCE_BREAK.finditer(text), None]:
        end = m.start() if m else len(text)
        words = len(text[pos:end].split())
        rows = numbers[bisect_right(starts, pos) - 1 : bisect_right(starts, max(end - 1, pos))]
        hit = rows[0] if allowed is None else next((n for n in rows if n in allowed), 0)
        if words > SENTENCE_WORD_CAP and hit:
            out.append(finding(hit, "sentence-length", words=words, cap=SENTENCE_WORD_CAP))
        pos = m.end() if m else pos
    return out


FIELD_LINE = re.compile(r"^\s*(?::\w+[^:]*:|\w+(?:\s*\([^)]*\))?:)(?:\s|$)")


def comment_indent(line: Line) -> int:
    """The columns a comment's text sits past its marker, or 0 where no raw form was kept."""
    at = line.raw.find(line.text) if line.text else -1
    lead = line.raw[: max(at, 0)].expandtabs(4)
    return len(lead) - len(lead.rstrip(" ")) if at > 0 else 0


def comment_sentences(unit: Unit, ctx: Context) -> list[Finding]:
    """Over-cap sentences in a comment run, a command or table indented under a colon skipped."""
    comments, _kept, configured = prose_comments(unit, ctx)
    out: list[Finding] = []
    block: list[tuple[int, str]] = []
    prev_no = reference = 0
    in_code = prev_colon = False
    for line in comments:
        n, body, leading = line.number, line.text, line.leading
        indent = comment_indent(line)
        if not body and leading:
            out += over_cap(block, ctx.allowed)
            block, prev_no = [], n
            continue
        follows = leading and n == prev_no + 1
        if is_non_prose_comment(line, configured, ctx.config):
            out += over_cap(block, ctx.allowed)
            block = []
            if follows and (in_code or prev_colon) and indent >= reference + INDENT_BLOCK:
                in_code, prev_no = True, n
                continue
            in_code = False
            prev_colon = leading and body.rstrip().endswith(":")
            if prev_colon:
                prev_no, reference = n, indent
            continue
        if not follows:
            in_code = prev_colon = False
        if follows and (in_code or prev_colon) and indent >= reference + INDENT_BLOCK:
            out += over_cap(block, ctx.allowed)
            block, in_code, prev_no = [], True, n
            continue
        in_code = False
        if not follows or LIST_MARKER.match(body) or FIELD_LINE.match(body) or body[:1].isupper():
            out += over_cap(block, ctx.allowed)
            block = []
        block.append((n, strip_list_marker(body)))
        prev_no, reference = n, indent
        prev_colon = body.rstrip().endswith(":")
        if not leading or prev_colon:
            out += over_cap(block, ctx.allowed)
            block = []
    return out + over_cap(block, ctx.allowed)


DOCSTRING_OPEN = re.compile(r"^\s*[rRuU]?(?:\"\"\"|\'\'\'|\"|\')")
DOCSTRING_CLOSE = re.compile(r"(?:\"\"\"|\'\'\'|\"|\')\s*$")
DOCTEST_PROMPT = re.compile(r"^\s*>>>(?:\s|$)")
NUMPY_HEAD_WORDS = 6
NUMPY_UNDERLINE = re.compile(r"^\s*-{3,}\s*$")
GOOGLE_ENTRY = re.compile(r"^\s*\*{0,2}\w[\w.]*(?:\s*\(.*?\))?:\s+(?=\S)")


def is_numpy_head(text: str, below: int, mid_sentence: bool = False, after_blank: int = -1) -> bool:
    """Whether a line at a NumPy entry's indent heads an entry rather than being prose."""
    words = text.split()
    indent = len(text) - len(text.lstrip())
    sentence = text.rstrip().rstrip("*_`\"')]").endswith((".", "!", "?", ":"))
    lone = len(words) == 1 and not words[0].rstrip("*_`\"')]").endswith((".", "!", "?"))
    short = after_blank > indent and len(words) <= NUMPY_HEAD_WORDS
    return (
        " : " in text
        or (below > indent and not sentence)
        or (not mid_sentence and (lone or (short and not sentence)))
    )


def indent_at(texts: list[str], idx: int) -> int:
    """The indent of `texts[idx]`, or -1 where it is blank or absent."""
    return leading_indent(texts[idx]) if idx < len(texts) and texts[idx].strip() else -1


def docstring_margin(unit: Unit, texts: list[str], closer: str) -> int:
    """The column later docstring lines are measured from, as `inspect.cleandoc` measures it."""
    quote = unit.column
    later = [
        len(t) - len(t.lstrip())
        for t in texts[1:]
        if t.strip() and not (leading_indent(t) < quote and BARE_URI.match(t.strip()))
    ]
    alone = len(texts) > 1 and closer.strip() and not texts[-1].strip()
    if (
        alone
        and leading_indent(closer) >= quote
        and (texts[0].strip() or leading_indent(closer) > quote)
    ):
        later.append(len(closer) - len(closer.lstrip()))
    elif alone and texts[0].strip():
        later.append(quote)
    return min(later) if later else quote


def docstring_sentences(unit: Unit, ctx: Context) -> list[Finding]:
    """Over-cap docstring sentences, its doctests, code blocks, and section structure skipped."""
    language = ctx.language
    numpy = {s.lower() for s in language.get("numpy_sections", ())}
    google, google_prose = (
        re.compile(r"^\s*(?:" + "|".join(map(re.escape, language.get(k, ()))) + r"):\s*$")
        for k in ("google_sections", "google_prose_sections")
    )
    texts = [line.text.expandtabs(4) for line in unit.lines]
    texts[0] = DOCSTRING_OPEN.sub("", texts[0], count=1)
    closer = texts[-1]
    texts[-1] = DOCSTRING_CLOSE.sub("", texts[-1], count=1)
    quote, base = unit.column, docstring_margin(unit, texts, closer)
    out: list[Finding] = []
    block: list[tuple[int, str]] = []
    in_doctest = in_code = prev_colon = False
    prev_blank = True
    section = ""
    section_indent = 0
    code_at = colon_ref = base
    skip = 0
    entry_indent: dict[int, int] = {}
    for idx, (n, text) in enumerate(zip((line.number for line in unit.lines), texts, strict=True)):
        if skip:
            skip -= 1
            continue
        indent = len(text) - len(text.lstrip()) if idx else min(quote, base)
        if not text.strip():
            out += over_cap(block, ctx.allowed)
            block, in_doctest, in_code, prev_blank = [], False, False, True
            colon_ref = max(colon_ref, base)
            continue
        following = texts[idx + 1] if idx + 1 < len(texts) else ""
        header = None
        if indent <= base and NUMPY_UNDERLINE.match(following) and not NUMPY_UNDERLINE.match(text):
            header, skip = ("numpy" if text.strip().lower() in numpy else ""), 1
            code_at = base + 2 * INDENT_BLOCK
        elif indent <= base and google.match(text):
            header, section_indent = "google", 0
        elif indent <= base and google_prose.match(text):
            header, section_indent = "text", 0
        if header is not None:
            out += over_cap(block, ctx.allowed)
            block, section = [], header
            in_doctest = in_code = prev_blank = prev_colon = False
            continue
        if section in ("google", "text") and indent <= base:
            section, code_at = "", base
        mid = bool(block) and entry_indent[block[-1][0]] <= base
        head = is_numpy_head(
            text,
            indent_at(texts, idx + 1),
            mid,
            indent_at(texts, idx + 2) if not following.strip() else -1,
        )
        if section == "numpy" and indent <= base and head:
            out += over_cap(block, ctx.allowed)
            block, in_doctest, in_code, prev_blank = [], False, False, False
            prev_colon = text.rstrip().endswith(":")
            continue
        if section in ("google", "text"):
            if not section_indent:
                section_indent = indent
                code_at = indent + (2 if section == "google" else 1) * INDENT_BLOCK
            lead = (
                GOOGLE_ENTRY.match(text)
                if section == "google" and indent <= section_indent
                else None
            )
            if lead:
                out += over_cap(block, ctx.allowed)
                block, text, indent, in_code = [], text[lead.end() :], section_indent, False
        threshold = code_at if section else base + INDENT_BLOCK
        colon_at = (section_indent if section in ("google", "text") else colon_ref) + INDENT_BLOCK
        if DOCTEST_PROMPT.match(text):
            out += over_cap(block, ctx.allowed)
            block, in_doctest = [], True
        elif in_doctest or (in_code and indent >= threshold):
            pass
        elif (prev_blank and indent >= threshold) or (prev_colon and indent >= colon_at):
            out += over_cap(block, ctx.allowed)
            block, in_code = [], True
        else:
            in_code = False
            if LIST_MARKER.match(text) or FIELD_LINE.match(text):
                out += over_cap(block, ctx.allowed)
                block = []
            block.append((n, strip_list_marker(text.strip())))
            entry_indent[n] = indent
            if text.rstrip().endswith(":"):
                out += over_cap(block, ctx.allowed)
                block = []
        prev_blank = False
        prev_colon = text.rstrip().endswith(":")
        colon_ref = quote if idx == 0 else base
    return out + over_cap(block, ctx.allowed)


def rule_sentence_length(unit: Unit, ctx: Context) -> list[Finding]:
    """A sentence over the word cap, in a Markdown block, a comment run, or a docstring."""
    if unit.kind == "paragraph":
        return over_cap(
            [(line.number, strip_list_marker(line.text)) for line in unit.lines], ctx.allowed
        )
    if unit.kind == "docstring":
        return docstring_sentences(unit, ctx)
    return comment_sentences(unit, ctx)


Rule = Callable[[Unit, Context], list[Finding]]

# Each rule and the unit kinds it reads, rules sharing a function sharing one pass over a unit.
# The rules from `home-path` on read line by line, so their findings are reported in line order.
RULE_TABLE: dict[str, tuple[Rule, frozenset[str]]] = {
    "comment-wrap": (rule_comment_wrap, frozenset({"comment", "markup"})),
    "comment-case": (rule_comment_wrap, frozenset({"comment", "markup"})),
    "comment-added": (rule_comment_added, frozenset({"comment"})),
    "issue-ref": (rule_issue_ref, frozenset({"prose", "comment", "doc", "docstring"})),
    "sentence-length": (rule_sentence_length, frozenset({"paragraph", "comment", "docstring"})),
    "home-path": (rule_home_path, frozenset({"source"})),
    "charset": (rule_charset, frozenset({"prose", "code"})),
    "charset-unknown": (rule_charset, frozenset({"prose", "code"})),
    "dead-path": (rule_dead_path, frozenset({"prose"})),
    "semicolon": (rule_semicolon, frozenset({"prose"})),
    "dash": (rule_dash, frozenset({"prose"})),
    "dupword": (rule_dupword, frozenset({"prose", "comment"})),
    "spelling": (rule_spelling, frozenset({"prose", "comment"})),
    "sentence-split": (rule_sentence_split, frozenset({"prose"})),
}
LINE_ORDERED = frozenset(list(RULE_TABLE)[list(RULE_TABLE).index("home-path") :])


def check_file(
    path: Path,
    rules: set[str],
    root: Path | None = None,
    allowed: set[int] | None = None,
    plain: list[Path] | None = None,
    undecoded: list[Path] | None = None,
) -> list[Finding]:
    """Every finding the rules in `rules` make on `path`.

    `plain` collects a file with no lexer and `undecoded` one that is not UTF-8, since each is
    otherwise counted as read with nothing found. A byte order mark is judged on its own.
    """
    try:
        decoded = path.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError):
        if undecoded is not None:
            undecoded.append(path)
        return []
    bom = chr(0xFEFF)
    text = decoded.removeprefix(bom)
    out = [f for f in charset_findings(1, bom) if f[1] in rules] if text != decoded else []
    lex = any(RULE_TABLE[r][1] - {"source", "prose", "code"} for r in rules)
    units, fenced, read_plain = extract(path, text, lex)
    if read_plain and plain is not None:
        plain.append(path)
    lexer = lexer_name(path, text.split("\n", 1)[0].rstrip("\r"))
    config = config_for(str(root) if root else None)
    ctx = Context(path, root, fenced, language_of(lexer) or {}, config, allowed)
    passes: dict[Rule, set[str]] = {}
    for name in RULE_TABLE:
        if name in rules:
            passes.setdefault(RULE_TABLE[name][0], set()).add(name)
    ordered: list[Finding] = []
    for func, names in passes.items():
        kinds = RULE_TABLE[next(iter(names))][1]
        hits = [f for u in units if u.kind in kinds for f in func(u, ctx) if f[1] in names]
        (ordered if names & LINE_ORDERED else out).extend(hits)
    return out + sorted(ordered, key=lambda f: f[0])


# The ASCII whitespace the composite action's trim removes, the smallest blank set in play.
# Judged by it, the gate never refuses an exclusion the action builds and passes.
ASCII_BLANK = " \t\n\r\v\f"


def refuse(message: str, **fields: Any) -> int:
    """Print why the run cannot give an honest verdict, and the exit code that says so."""
    print(printable("error: " + say(message, **fields)), file=sys.stderr)
    return 2


def note(message: str, **fields: Any) -> None:
    print("note: " + say(message, **fields), file=sys.stderr)


def stand_downs(rules: set[str], a: argparse.Namespace, scan_root: Path, in_git: bool) -> None:
    """Drop a rule this run cannot honestly run, announced, since a quiet stand-down reads as a pass."""
    by_env = os.environ.get(COMMENT_ENV_NAME, "").strip().lower() not in COMMENT_ENV_OFF
    if "comment-added" in rules and (a.allow_comments or by_env or a.diff is None):
        rules.discard("comment-added")
        named = COMMENT_ENV_NAME if by_env and not a.allow_comments else "--allow-comments"
        note("note-allowed" if a.allow_comments or by_env else "note-no-diff", named=named)
    if "dead-path" in rules and in_git and shallow_checkout(scan_root):
        rules.discard("dead-path")
        note("note-shallow")


def argument_parser(data: dict[str, Any]) -> argparse.ArgumentParser:
    """The command line the data file declares, which `scripts/prose_lint.py` builds the same way."""
    ap = argparse.ArgumentParser(prog="prose_lint.py")
    for flag, spec in data["cli"].items():
        choices = {"choices": sorted(data["rules"])} if spec.get("choices") == "rules" else {}
        ap.add_argument(flag, **{**spec, **choices})
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # A path holding a byte that is not UTF-8 prints escaped rather than ending the run.
        if callable(reconfigure := getattr(stream, "reconfigure", None)):
            reconfigure(errors="backslashreplace")
    a = argument_parser(BASE).parse_args(argv)
    rules = set(a.checks or DEFAULT_RULES)
    # Every input to a verdict is read from the repository scanned, never from the working directory.
    # TestScanRootDecidesTheRuleSet carries the cases.
    scan_paths = a.paths or ["."]
    # An empty exclusion matches every key and empties the scan while the run exits 0.
    if any(not x.strip(ASCII_BLANK) for x in a.exclude):
        return refuse("error-empty-exclude")
    # Anything that is not a file or a directory, a FIFO or a socket included, is refused.
    if unusable := [p for p in scan_paths if not (Path(p).is_file() or Path(p).is_dir())]:
        return refuse("error-not-a-path", paths=quoted(unusable))
    git_roots = {found for found in (repo_root(Path(p)) for p in scan_paths) if found}
    if len(git_roots) > 1:
        return refuse("error-many-repositories", roots=quoted(git_roots))
    first, in_git = Path(scan_paths[0]), bool(git_roots)
    scan_root = Path(git_roots.pop()) if git_roots else first if first.is_dir() else first.parent
    try:
        config_for(str(scan_root))
    except OverrideError as exc:
        return refuse("error-override", path=scan_root / OVERRIDE_NAME, why=exc)
    stand_downs(rules, a, scan_root, in_git)

    files = discover(scan_paths, tuple(a.exclude), scan_root)
    keys = {f: repo_key(f, scan_root) for f in files}
    if a.list_files:
        for f in files:
            print(printable(keys[f]))
        return 0
    discovered = len(files)
    # Presence rather than truthiness, since an empty value is a base that failed to compute.
    scope = changed_lines(a.diff, scan_root) if a.diff is not None else None
    if a.diff is not None and scope is None:
        return refuse("error-no-diff", base=a.diff)
    if scope is not None:
        matched = [f for f in files if keys[f] in scope]
        # A non-empty diff whose readable files the scan matched none of failed to scope.
        unread = (
            [] if matched else unread_diff_files(scope, scan_paths, tuple(a.exclude), scan_root)
        )
        if unread:
            fields = {"base": a.diff, "count": len(unread), "paths": quoted(unread, 5)}
            return refuse("error-unread-diff", **fields)
        files = matched

    total = 0
    bykind: dict[str, int] = {}
    byfile: dict[str, int] = {}
    plain: list[Path] = []
    undecoded: list[Path] = []
    for f in files:
        allowed = scope.get(keys[f]) if scope is not None else None
        for ln, kind, msg in check_file(f, rules, scan_root, allowed, plain, undecoded):
            if allowed is not None and ln not in allowed:
                continue
            total += 1
            bykind[kind] = bykind.get(kind, 0) + 1
            byfile[keys[f]] = byfile.get(keys[f], 0) + 1
            if not a.summary:
                print(printable(f"{keys[f]}:{ln}: {kind}: {msg}"))
    inscope = sum(len(scope[keys[f]]) for f in files) if scope is not None else None
    provenance = gate_provenance(a.provenance)
    named = [[keys[f] for f in group] for group in (plain, undecoded)]
    note_line = scope_note(len(files), discovered, inscope, a.diff, provenance, *named)
    print(printable(note_line), file=sys.stderr)
    if a.summary or total:
        print(f"\n{total} violation(s) across {len(byfile)} file(s)", file=sys.stderr)
        for k, v in sorted(bykind.items(), key=lambda kv: -kv[1]):
            print(f"  {k:16} {v}", file=sys.stderr)
        for k, v in sorted(byfile.items(), key=lambda kv: -kv[1])[:15] if a.summary else ():
            print(printable(f"  {v:5}  {k}"), file=sys.stderr)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
