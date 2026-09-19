#!/usr/bin/env python3
"""Enforce the GOVERNANCE.md "Documentation Style Conventions" rules no other linter checks.

markdownlint, cspell, actionlint, and editorconfig-checker all pass on prose that breaks
these rules, so nothing enforced them before this script. Rules implemented:
  charset        Non-ASCII judged against the three tiers the charset rule defines.
  charset-unknown A non-ASCII character in no tier, so it is classified rather than assumed.
  semicolon      No semicolon in prose, outside a list that already carries commas.
  dash           No spaced hyphen joining or interrupting a sentence.
  comment-wrap   One sentence per comment line, never wrapped and never two on a line.
  comment-case   A comment sentence starts with a capital, not a lowercase word.
  comment-added  No comment line opening its own line added or edited, unless the change says so.
  dupword        No duplicated consecutive word.
  sentence-split A sentence must not wrap across lines (one sentence per line).
  sentence-length A Markdown prose sentence must not exceed the word cap.
  spelling       No British spelling, the repo-wide convention being US English.
  home-path      No absolute home path naming a real account, per the representative-data rule.
  dead-path      No mention of a path git once tracked and the tree no longer holds.

Exit 1 if any violation is found. Read-only, never edits.

Every git read below names its encoding rather than taking the locale's. On Windows the
locale encoding is the ANSI code page, which cannot map every byte git emits, and the
handlers around these calls catch no decode failure. Surrogateescape rather than strict, so
such a byte does not end the read either. A name round-trips to the same name on disk, and a
diff's content reaches no further than the hunk headers this file reads out of it.
"""

from __future__ import annotations

import argparse
import ast
import functools
import io
import json
import os
import re
import subprocess
import sys
import tokenize
import unicodedata
from pathlib import Path
from typing import NamedTuple, TypedDict

# One source of truth for the rule names, so the CLI choices cannot drift from check_file.
RULES = {
    "charset": "a non-ASCII character its tier does not permit here",
    "charset-unknown": "a non-ASCII character in no tier",
    "semicolon": "a semicolon in prose, outside a list that already carries commas",
    "dash": "a spaced hyphen joining or interrupting a sentence",
    "comment-wrap": "a comment sentence wrapped across lines, or two on one line",
    "comment-case": "a comment sentence opening in lowercase",
    "comment-added": "a comment line this change adds or edits, opening its own line",
    "dupword": "a duplicated consecutive word",
    "sentence-split": "a sentence wrapping across lines",
    "sentence-length": "a sentence over the word cap",
    "spelling": "a British spelling where the repo convention is US English",
    "home-path": "an absolute home path naming a real account",
    "dead-path": "a mention of a path git once tracked and the tree no longer holds",
    "issue-ref": "an issue or pull request reference in a comment, a docstring, or instruction text",
}
DEFAULT_RULES = frozenset(
    {
        "charset",
        "charset-unknown",
        "semicolon",
        "dash",
        "dupword",
        "spelling",
        "comment-wrap",
        "comment-case",
        "comment-added",
        "home-path",
        "dead-path",
        "issue-ref",
    }
)

# Trees this repo generates rather than authors, skipped when a wider scan expands into them.
# The gate then measures hand-written prose.
# `spec/audit.py` writes `reports/`, so a finding there is the engine's phrasing, not an author's.
# No edit to that tree can fix one.
# Naming one of these paths directly still reads it, so nothing becomes uncheckable.
GENERATED_TREES = frozenset({"reports"})

# Produced rather than authored trees, consulted only on the no-git fallback path.
# Where git can answer, its own ignore rules are the better answer.
GENERATED_ROOTS = frozenset(
    {
        ".git",
        ".artifacts",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "bin",
        "obj",
        "dist",
    }
)

# A floor on what a healthy sweep of this repo reaches, asserted by the tests.
# A sweep that quietly stops finding files satisfies every rule by having nothing to read.
LEAST_PLAUSIBLE = 60

# The pattern-detectable half of the representative-data rule, and only that half.
# A real user segment is required, so a documented placeholder describes the shape unmatched.
# That is how the rule's own wording escapes its own gate, with no exemption naming files.
# A bare drive letter is deliberately not a shape here.
# Measured against this repo it matched 11 files and named a path in none of them.
# An escaped newline after a word ending in a letter and a colon reads as a drive letter.
# `Users` is matched case-insensitively on the Windows branch alone, since that filesystem is.
# The POSIX branches stay case-sensitive, since a lowercase `/users/` is a common REST path.
# An API route is not a home directory, and widening this would flag one in every doc.
HOME_PATH = re.compile(
    r"(?:/home/|/Users/|[A-Za-z]:\\(?i:users)\\)(?P<user>[A-Za-z][A-Za-z0-9._-]*)"
)

# Accounts that belong to a container or a runner rather than to a person.
# Every one is a fixed name an image ships, so a path under it names no environment.
# `vscode` is the devcontainer user this repo's own snippets mount into.
# `runner` is the GitHub Actions user, and the rest are stock image accounts.
SERVICE_ACCOUNTS = frozenset({"vscode", "runner", "root", "ubuntu", "node", "shared", "public"})


def rel(path: Path) -> str:
    """The repo-relative posix key a git diff uses for this path.

    `removeprefix` rather than `lstrip`, which takes a character set and ate the leading dot of
    every dotfile - it turned `.github/workflows/x.yml` into `github/workflows/x.yml`, so --diff
    could never match a path under a dot directory.
    """
    return path.as_posix().removeprefix("./")


def repo_key(path: Path, root: Path) -> str:
    """The repository-relative posix key that joins a scanned file to a diff entry.

    A diff names every file relative to the repository top level, while a path argument arrives
    absolute, relative to a subdirectory, or dotted. The two are compared here and nowhere else,
    since comparing them in whatever form each happened to arrive in is what let an absolute path
    argument match no diff entry at all and report a clean run over a tree it had read in full.

    A path outside `root` has no repository-relative form and keeps its own, so nothing in a diff
    can match it. That is the honest answer rather than a coincidental one.
    """
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return rel(path)


def all_lines(path: Path) -> set[int]:
    """Every line number in `path`, which is the changed scope of a file that is entirely new."""
    try:
        return set(range(1, len(path.read_bytes().splitlines()) + 1))
    except OSError:
        return set()


def untracked_paths(root: Path) -> list[str]:
    """Paths relative to `root` that git holds no history for and is not ignoring.

    They are repository-relative only where `root` is the repository top level, which is how
    `changed_lines` calls it, since a diff key is repository-relative. `discover` passes the
    directory it was asked about and joins the names onto it, so both readings hold at once.

    An untracked file is the whole of what a change adds and `git diff` never names one, so a
    scope built from the diff alone reads a new file as absent rather than as new. `git ls-files`
    omits it too, so this is not a diff-mode quirk: a whole-tree sweep passed over it as well.
    Ignored paths stay out, since a build output is not authored text.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except (OSError, ValueError):
        return []
    if r.returncode != 0:
        return []
    return [name for name in r.stdout.split("\0") if name]


def diff_header_path(field: str) -> str | None:
    """The repository-relative path a `+++` header names, or None where it names no file.

    Git quotes a name holding any byte at or above 0x80, and one holding a quote, a backslash,
    or a control character, escaping it the way C does. Reading the quoted form as a literal path
    dropped the file from scope, and an empty scope is falsy, so `main`'s no-match refusal did
    not fire either and the run reported a clean gate on a file it never read.

    The caller pins `core.quotePath=true`, which is what makes a quoted field ASCII and so what
    this decode assumes. Turning the setting off instead would not do: it stops git quoting the
    first of those three routes and leaves the other two quoting a name whose non-ASCII bytes sit
    raw inside the quotes, which this decode cannot carry.

    A header naming no `b/` path adds nothing to scope, which is how a deletion's `/dev/null`
    leaves the caller with no file to credit the hunk that follows it.

    Git appends a literal tab to mark where a path holding a space ends, quoted or not, and
    escapes a literal tab inside the name itself as `\\t` rather than ever emitting it raw. A
    tab in the field is therefore always that terminator, so the field is truncated at its first
    tab before the quoting check runs, letting a spaced name reach this decode the same as one
    with no space in it.
    """
    tab = field.find("\t")
    if tab != -1:
        field = field[:tab]
    if field.startswith('"') and field.endswith('"') and len(field) > 1:
        # Latin-1 round-trips each byte, so a raw byte the escape carries survives the decode.
        unescaped = field[1:-1].encode("latin-1", "backslashreplace").decode("unicode-escape")
        field = unescaped.encode("latin-1", "surrogateescape").decode("utf-8", "surrogateescape")
    return field[2:] if field.startswith("b/") else None


def undiffable_paths(base: str, root: Path) -> set[str] | None:
    """Repository-relative paths, `-C root` diffed against `base`, that git declines to diff.

    `git diff --numstat` reports `-` for both the added and removed counts on a path it treats as
    binary, whether genuinely binary or marked `-diff`, and in patch mode emits no `+++` header
    for such a path at all, only a `Binary files ... differ` prose line naming it. That prose
    joins the two paths with " and ", so a name holding that exact substring cannot be split back
    into its two paths without guessing, and a name a shell would quote fares no better either.
    `-z` sidesteps both problems: every path arrives NUL-terminated and never quoted, whatever
    bytes it holds, so nothing here needs decoding beyond the raw diff output itself.

    A renamed or copied path's own record carries no path at all, an empty field where one
    normally sits, and the old and new paths follow as two further NUL-terminated fields in that
    order. The new path is the one kept, since that is where the working tree holds the file now.

    None if git fails, the same failure `changed_lines` already handles for its own invocation.
    """
    try:
        raw = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--numstat",
                "-z",
                # Matches the main invocation's own guards, so the two agree on what is binary.
                "--no-ext-diff",
                "--no-textconv",
                base,
                "--",
            ],
            capture_output=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    paths: set[str] = set()
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        added, _, rest = record.partition("\t")
        removed, _, path = rest.partition("\t")
        if not path:
            # A rename's two trailing fields are the old path and the new one, in that order.
            # A stream ending between them is refused rather than parsed on, since dropping the records after it would under-report exactly the paths this function exists to name.
            if i + 1 >= len(fields):
                return None
            i += 1  # the old path, unused since the new path is where the content now lives
            path = fields[i]
            i += 1
        if added == "-" and removed == "-":
            paths.add(path)
    return paths


def changed_lines(base: str, root: Path) -> dict[str, set[int]] | None:
    """Map repository-relative path -> line numbers this working tree adds vs `base`.

    None if git fails. The diff is taken at `root`, the repository being scanned, rather than
    wherever the process happens to stand. Keys anchored on the working directory match nothing
    once the two differ, and a `diff.relative` setting would re-anchor them the same way.

    An untracked file counts as added in full, since a change whose whole point is adding a file
    otherwise scopes to nothing and reports a clean run on exactly the file it added.

    A name git quotes even with `core.quotePath=false`, one holding a quote, a backslash, or a
    control character, is unquoted here rather than left to miss the same way.

    Captured as bytes and decoded explicitly rather than read in text mode, since text mode's
    universal-newline translation turns a lone `\\r` inside added content into a line break of
    its own, letting one added line forge a second `+++` or `@@` header the parse below then acts on.

    A `+++` header naming a path that exists in the working tree and that `is_text` rejects
    builds no entry for it. A `diff` attribute can force git to treat a genuinely binary file as
    text, emitting that header and a full range of hunks for it the same as for a text file, and
    `discover` and `unread_diff_files` both drop such a path later regardless, so the verdict does
    not change, but the line numbers would otherwise have already been accumulated for a file
    this gate can never read. A path that does not exist in the working tree is left in scope
    rather than dropped, so a deleted or otherwise unreadable file this gate could still credit
    before this check keeps being credited.

    Without `--text`, git reports no `+++` header at all for a path it declines to diff, a real
    binary or one a `-diff` attribute marks the same way, so no hunk ever adds it to `out` through
    the loop below. `undiffable_paths` names those paths from a second invocation, and `is_text`
    then tells apart the two reasons git could have declined one: a `-diff` attribute on a file
    that reads as text, which is credited in full since no hunk will describe its lines, and a
    file that is genuinely binary, which is left out of the map.
    """
    try:
        raw = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                # `diff_header_path` decodes a quoted name as ASCII, and this is what makes it so.
                # An inherited `false` leaves a high byte raw inside the quotes git still adds.
                # That field is then not ASCII, and the decode either corrupts it or raises.
                "-c",
                "core.quotePath=true",
                "diff",
                "--unified=0",
                # Outranks diff.interHunkContext, whose nonzero host default merges nearby hunks and pulls the unchanged lines between them into scope.
                "--inter-hunk-context=0",
                "--no-color",
                "--ignore-cr-at-eol",
                # `--src-prefix=a/` pins diff.srcPrefix for symmetry with the dst side and has no parse consequence, since only `+++` is read.
                "--src-prefix=a/",
                # Pins diff.dstPrefix, and outruns diff.noprefix and diff.mnemonicPrefix, so `+++` keeps naming a `b/` this parse keys on.
                "--dst-prefix=b/",
                # Bypasses diff.external, which would otherwise replace the parsed body with an arbitrary command's output.
                "--no-ext-diff",
                # Disables textconv, on by default for a porcelain diff, whose driver can shift every line number this parse reads.
                "--no-textconv",
                base,
                "--",
            ],
            capture_output=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    undiffable = undiffable_paths(base, root)
    if undiffable is None:
        return None
    d = raw.decode("utf-8", "surrogateescape")
    out: dict[str, set[int]] = {}
    cur = None
    # A `+++` or `@@` line is honored only while it opens the block a `diff --git` line just started.
    # Content can never open a line with `diff --git `, so it can never forge that opening either.
    in_header = False
    for line in d.split("\n"):
        if line.startswith("diff --git "):
            in_header = True
            cur = None
            continue
        if in_header and line.startswith("+++ "):
            in_header = False
            cur = diff_header_path(line[4:])
            if cur is None:
                continue
            target = root / cur
            if target.is_file() and not is_text(target):
                # A `diff` attribute can force git to emit a full patch for a genuinely binary file, so this stays reachable even though most binaries never reach `+++` at all now.
                # Dropped here rather than left as an empty entry, since `discover` and `unread_diff_files` would drop it later anyway.
                cur = None
                continue
            out.setdefault(cur, set())
        elif line.startswith("@@") and cur:
            in_header = False
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or 1)
                out[cur].update(range(start, start + count))
    for path in undiffable:
        target = root / path
        if is_text(target):
            # No hunk will describe this path, since git declined to diff it, so credit it whole.
            out[path] = all_lines(target)
    for name in untracked_paths(root):
        target = root / name
        if is_text(target):
            out[name] = all_lines(target)
    return out


def asked_about(key: str, paths: list[str]) -> bool:
    """Whether a repository-relative diff key falls under one of the requested paths.

    The floor below compares the diff's file list against what the run matched, and a caller who
    narrowed the scan on purpose must not be told the narrowing is a defect. Anything the request
    did not cover is not a file this run failed to read.
    """
    for raw in paths:
        # `Path` drops a trailing separator, so the test appends one rather than stripping it.
        # Comparing the bare prefix would let `catalog` claim `catalogue/x.md`.
        r = rel(Path(raw))
        if r in ("", "."):
            return True
        if key == r or key.startswith(r + "/"):
            return True
    return False


def unread_diff_files(
    scope: dict[str, set[int]], paths: list[str], excludes: tuple[str, ...], root: Path
) -> list[str]:
    """Files the diff names that this run was asked about and could have read, in sorted order.

    Both sides are read against `root`, the repository being scanned, because `git diff` reports
    repository-relative paths while a request arrives in whatever form the caller typed. Reading
    either against the working directory empties this list from a subdirectory and empties it for
    an absolute path argument, which are the two places it most needs to be full.
    """
    asked = [repo_key(Path(p), root) for p in paths]
    out: list[str] = []
    for key in sorted(scope):
        if not asked_about(key, asked):
            continue
        if any(x in key for x in excludes):
            continue
        if not GENERATED_TREES.isdisjoint(Path(key).parts):
            continue
        target = root / key
        if target.is_file() and is_text(target):
            out.append(key)
    return out


def gate_provenance(explicit: str | None = None) -> str:
    """Which copy of this gate produced the verdict, named so a finding can be attributed to one.

    A repository reaches this gate through a pinned hub commit, so the copy CI runs is the one
    that pin names rather than the one a hub checkout holds. The two disagree from the moment a
    rule changes until the pin moves, and a finding raised by the older copy then reproduces
    against no local run at all. Unattributed, that reads as a defect in the gate rather than as
    a version gap, which is the reading it actually got: three dead-path findings were carried
    into a resync as real work, and the pin was a single commit behind the exemption that
    silenced them.

    Three sources answer, in the order they can be trusted. An explicit value is the composite
    action stating its own `owner/repo@ref`, which is the only source that knows the pin, since
    a checked-out action carries no history of its own. The environment carries the same value
    for a caller that invokes the script directly. Otherwise this script's own checkout answers,
    under the conditions `checkout_provenance` states. None of the three resolving is reported as
    unknown rather than guessed, since a wrong attribution is worse here than an absent one: it
    sends the next investigation at a copy nobody ran, where no attribution at least leaves the
    question open.
    """
    # Stripped before it is tested rather than after, since a whitespace-only value is truthy and would otherwise pass the test and then empty itself, printing the label with nothing after it.
    # A source carrying no value falls through to the next one, which is what carrying none means.
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("PROSE_GATE_PROVENANCE", "").strip()
    if env:
        return env
    return checkout_provenance(Path(__file__).resolve())


def checkout_provenance(script: Path) -> str:
    """The commit that describes this file's content, or unknown where no commit does.

    A checkout containing the script is not the same fact as a commit describing it, and reading
    HEAD alone conflates the two. Dropping a copy into an unrelated repository resolves that
    repository's HEAD, which is a well-formed answer naming content it never held. Editing the
    file in place resolves the commit before the edit, which is the ordinary state of any branch
    that changes a rule, so the run that most needs an accurate attribution is the one that would
    get a stale one.

    Both are narrowed by asking what HEAD says about this path rather than what it says about the
    repository: an untracked path is attributed to nothing, and a tracked path whose working copy
    has moved is named as moved rather than as its commit.

    What this does not establish is which repository answered. A copy committed into an unrelated
    repository is tracked and clean there, so it is named by that repository's HEAD, and the value
    carries no identity to tell it apart from a hub checkout's own. A `local` value is therefore
    read as a commit inside the repository that produced it and nowhere else, which is what the
    explicit and environment sources exist to do better. Closing it properly means naming the
    repository alongside the commit, and that is a change to the value's shape rather than to
    this resolution.

    The third way they come apart is the environment rather than the filesystem. Git's location
    variables outrank `-C`, and a git hook exports them, so a run made from inside one answers
    about whichever repository invoked the hook rather than about the copy that is running. That
    is the same wrong attribution arriving by a different route, so the location variables are
    dropped and the answer keys on the script's own path alone.
    """
    located_by_environment = (
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    )
    env = {k: v for k, v in os.environ.items() if k not in located_by_environment}

    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", "-C", str(script.parent), *args],
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

    if git("ls-files", "--error-unmatch", "--", str(script)) is None:
        return "unknown"
    head = (git("rev-parse", "--short", "HEAD") or "").strip()
    if not head:
        return "unknown"
    moved = (git("status", "--porcelain", "--", str(script)) or "").strip()
    return f"local {head}-dirty" if moved else f"local {head}"


def scope_note(
    read: int, discovered: int, lines: int | None, base: str | None, provenance: str
) -> str:
    """What the run actually read, stated on every verdict rather than only on a busy one.

    Five routes to a false clean are on record and every one of them exits 0 in silence: an
    unresolvable base widening to a whole-tree scan, a diff taken in one repository while scanning
    another, a path under no repository, an absolute path argument whose keys matched no diff
    entry, and an untracked file no diff names. Each guard so far closes the route a reviewer
    happened to see, and the sixth is found that way or not at all. What every one of them shares
    is that a scope of nothing prints exactly what a clean tree prints, which is a property of the
    output and not of any single route. Stating the scope is what a reader needs to tell "read
    nothing" from "found nothing", so it is printed even when the count is the whole tree.

    The copy that read that scope is named beside it for the same reason. A scope answers what
    was read and a provenance answers what read it, and a verdict reproducing nowhere is a
    question about the second rather than about the first.
    """
    if base is None:
        return f"scope: {read} file(s) read, whole tree, gate {provenance}"
    return (
        f"scope: {read} of {discovered} file(s) read, {lines} changed line(s), "
        f"diff against {base!r}, gate {provenance}"
    )


def home_path_findings(lineno: int, line: str) -> list[tuple[int, str, str]]:
    """Absolute home paths on this line that name a real account.

    The exposure this gates was a maintainer's own path reaching a public comment, so the unit
    is the raw line rather than stripped prose. A path is the same exposure in a JSON config
    value, in a fenced transcript pasted from a terminal, and in a sentence.
    """
    out = []
    for m in HOME_PATH.finditer(line):
        if m.group("user").lower() in SERVICE_ACCOUNTS:
            continue
        out.append(
            (
                lineno,
                "home-path",
                (
                    f"absolute home path {m.group(0)!r} -> use a constructed path, not an "
                    "observed one"
                ),
            )
        )
    return out


# The named-path half of the stale-description class, and only that half (RESYNC.md section 4).
# The measured incident named no path at all, and no pattern reaches a description without one.
# A backtick span, an inline link target, and a reference definition each assert a path.
INLINE_SPAN = re.compile(r"`([^`\n]+)`")
LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")
REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s+(\S+)")
# A character that marks a token as a placeholder, a glob, an expansion, or a scheme.
# The colon covers every URL scheme, a drive letter, and an image tag in one stroke.
PATH_FOREIGN = frozenset("<>{}$*?\"'\\:!|,;")


def path_candidate(token: str, in_span: bool = True) -> str | None:
    """The relative path a token asserts, or None when it asserts none.

    A backtick span holds prose as often as a path, so it qualifies only when it is shaped
    like a file: a single word carrying a separator and a suffix, which is what tells
    `spec/audit.py` from a ref like `origin/develop` and from a bare directory pattern like
    `references/`, a shape docs use for any repository's layout rather than this one's. A
    link target or a reference definition is a path by construction, so only a foreign
    character disqualifies it there.
    """
    token = token.split("#", 1)[0]
    if not token or any(c.isspace() for c in token) or PATH_FOREIGN & set(token):
        return None
    if token.startswith(("/", "~", "-", "#")):
        return None
    if in_span:
        if "/" not in token:
            return None
        last = token.rsplit("/", 1)[-1]
        if "." not in last or not last.strip("."):
            return None
    return token.removeprefix("./")


# The ASCII whitespace the composite action's trim always removes from an exclusions line, whatever locale it runs under.
# Its `[[:space:]]` is a superset of this in a UTF-8 locale and Python's own `str.strip` is a superset again, so judging blank by either would refuse a value the action does build and pass.
# Naming the smallest of the three keeps the divergence one way, which is the direction where the guard cannot fail a repository over an exclusion that is merely inert.
ASCII_BLANK = " \t\n\r\v\f"

# Paths with a `retire` disposition remain valid references to a hub-hosted tool or a declared deletion.
# The action fetches this file without the hub tree, so a test keeps this literal set equal to the ledger.
HUB_HOSTED = frozenset(
    {
        "repo-config/configure.sh",
        "repo-config/develop.json",
        "repo-config/operational/develop.json",
        "repo-config/main.json",
        "repo-config/README.md",
        "repo-config/settings.json",
        "repo-config/labels.json",
        "spec/secrets.json",
        ".github/workflows/get-version-task.yml",
        ".github/workflows/publish-plan-task.yml",
        ".github/workflows/build-release-task.yml",
        ".github/workflows/build-docker-task.yml",
        ".github/workflows/validate-task.yml",
        ".github/workflows/deploy-site-task.yml",
        ".github/workflows/publish-docker-readme-task.yml",
        ".github/workflows/check-upstream-version-task.yml",
        ".github/workflows/run-codegen-pull-request-task.yml",
        ".github/workflows/build-datebadge-task.yml",
    }
)


@functools.cache
def carried_paths(root: str) -> frozenset[str]:
    """Paths the manifest declares as carried, exempt because docs name them as fleet layout.

    The hub's own instance of a carried file retires to a catalog snippet, so its history
    reads as a deletion while every mention legitimately describes the file a repository
    carries. Whether a repository actually carries one is the audit's finding, not prose's.
    """
    try:
        data = json.loads((Path(root) / "spec" / "files.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(
        e["path"]
        for e in data.get("baseline", [])
        if isinstance(e, dict) and isinstance(e.get("path"), str)
    )


@functools.cache
def once_tracked(root: str, rel_path: str) -> bool:
    """Whether git at `root` ever recorded `rel_path`, the deletion signature this rule keys on."""
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%H", "--", rel_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except (OSError, ValueError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def dead_path_findings(
    root: Path, base: Path, lineno: int, line: str
) -> list[tuple[int, str, str]]:
    """Named paths on this line that git once tracked and the tree no longer holds.

    Requiring a history is what scopes this to the deletion-sweep shape, a file removed with
    its describing prose left standing. A path another repository holds, a proposed file a
    backlog names, and a layout pattern each have no history here, so none is reported. A
    carried path and a hub-hosted one are exempt with a history, since each names a file that
    lives elsewhere by design rather than a description this tree left behind.
    """
    m = REF_DEF.match(line)
    if m:
        # A definition line holds one target and no prose, so nothing else on it is read.
        tokens = [(m.group(1), False)]
    else:
        tokens = [(s.group(1), True) for s in INLINE_SPAN.finditer(line)]
        tokens += [(t.group(1), False) for t in LINK_TARGET.finditer(strip_inline_code(line))]
    out = []
    for token, in_span in tokens:
        rel_path = path_candidate(token, in_span)
        if rel_path is None:
            continue
        # A mention is anchored where it resolves, the file's own directory or the root.
        if (root / rel_path).exists() or (base / rel_path).exists():
            continue
        for anchor in {root, base}:
            try:
                tracked_rel = (anchor / rel_path).resolve().relative_to(root.resolve())
            except ValueError:
                continue
            # Both exemption sets are keyed by the posix path the manifest and the ledger hold.
            # A git pathspec is posix too, which `rel` already relies on for the diff scope.
            # So one key serves both, rather than the platform's separator reaching either.
            key = tracked_rel.as_posix()
            if key in carried_paths(str(root)) or key in HUB_HOSTED:
                continue
            if once_tracked(str(root), key):
                out.append(
                    (
                        lineno,
                        "dead-path",
                        (
                            f"path {token!r} is deleted from this tree -> re-point, rewrite, "
                            "or remove the stale mention"
                        ),
                    )
                )
                break
    return out


def shallow_checkout(root: Path) -> bool:
    """Whether the checkout at `root` is shallow, which holds no deletion history to key on."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except (OSError, ValueError):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def is_operations_runbook(path: Path, root: Path | None) -> bool:
    """Whether this path is the repository operations runbook."""
    return root is not None and path.resolve() == (root / "OPERATIONS.md").resolve()


# The pattern-detectable half of the reference ban, and only that half.
# An issue or a pull request reference has one shape, and a commit does not.
# A pinned action is a bare SHA a workflow rule requires, so reading one would report that pin.
ISSUE_REF = re.compile(r"(?<!#)#[0-9]{2,6}(?!\w)")

# Markdown an agent reads as instructions, where the ban reaches the document and not only its comments.
# A skill is law wherever it loads, and these documents are the fleet's own rule text.
# A README, a tracker, a history, and a plan are the repository's own narrative and keep their references.
INSTRUCTION_DOCS = frozenset(
    {
        "AGENTS.md",
        "AUDIT.md",
        "CLAUDE.md",
        "CODESTYLE.md",
        "GOVERNANCE.md",
        "OPERATIONS.md",
        "RESYNC.md",
        "STANDUP.md",
        "WORKFLOW.md",
        ".github/copilot-instructions.md",
    }
)


def is_instruction_text(path: Path, root: Path | None) -> bool:
    """Whether this Markdown file is instruction text rather than the repository's own narrative.

    A skill is matched by its directory rather than by its name, since the source tree and each
    generated distribution carry the same file under three roots.
    """
    if path.suffix.lower() != ".md":
        return False
    key = repo_key(path, root) if root is not None else rel(path)
    return key in INSTRUCTION_DOCS or "skills" in key.split("/")[:-1]


def quoted(paths) -> str:
    """Paths as a sorted, quoted, comma-joined list for an error message.

    Quoted because a path holding a space or a comma is indistinguishable from two paths once
    joined, which makes the message unreadable exactly when it names something unexpected.
    """
    return ", ".join(repr(str(p)) for p in sorted(paths))


def repo_root(path: Path) -> str:
    """The repository top level containing `path`, or '' when git cannot say."""
    start = path if path.is_dir() else path.parent
    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except (OSError, ValueError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def tracked_paths(root: Path) -> list[Path] | None:
    """Paths git tracks under `root`, or None when git cannot answer.

    Empty output is a None too. `git ls-files` succeeds with no output in an initialized but
    empty checkout, and reading that as an empty file set would scan nothing and report success.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except (OSError, ValueError):
        return None
    if r.returncode != 0 or not r.stdout.strip("\0"):
        return None
    return [root / name for name in r.stdout.split("\0") if name]


def walk_paths(root: Path) -> list[Path]:
    """Every file under `root` minus the generated trees, for a checkout git cannot describe.

    `git check-ignore` fails on exactly the machine that has no git, so this path asserts the
    generated-root rule by name instead of asking git which paths are ignored.
    """
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


def discover(
    paths: list[str], excludes: tuple[str, ...] = (), root: Path | None = None
) -> list[Path]:
    """Every authored text file the rules govern, scoped by git where git can answer.

    Where it cannot, the fallback walk applies no ignore rules at all and asserts the generated
    roots by name instead, so the scoping there is weaker than the paragraph below describes. It
    warns on stderr, since a quieter file set that reads the same is how a sweep stops covering
    what it claims to.

    The line-endings rule already requires a repo-wide sweep be scoped to `git ls-files` rather
    than a directory list, which covers what its author thought of and silently stops covering
    whatever is added next. An extension allowlist has that same defect, so the filter here is
    whether the file is text, not whether its suffix was thought of. An untracked file is authored
    text the same way, and reading only the tracked list passed over a new file until it was
    staged, so a clean sweep proved nothing about the one file a change existed to add.

    `root` is the repository every path is judged against, passed in so that the caller's verdict
    and this file set are keyed alike. Judging against the filesystem path instead lets a
    directory *above* the checkout decide: a repository cloned under a parent named `reports` had
    its own `reports/` tree scanned as authored, and an absolute argument carried its whole parent
    chain into every `--exclude` test.

    An explicit file argument bypasses discovery, so a single file can always be checked directly.
    """
    found: list[tuple[Path, str]] = []
    for raw in paths:
        p = Path(raw)
        base = p if p.is_dir() else (p.parent if p.is_file() else Path("."))
        anchor = root if root is not None else Path(repo_root(base) or base)
        if p.is_file():
            found.append((p, repo_key(p, anchor)))
            continue
        candidates = tracked_paths(base)
        # `tracked_paths` answers None for a tree git cannot describe and for an empty answer.
        # Only the first of those justifies a walk.
        # Whether git can describe a tree is settled by asking git, never by its answer's size.
        # Read as emptiness, a subtree of new files fell back and scanned the ignored ones under it.
        # It also printed that git could not describe a tree git describes fine.
        if candidates is None and not repo_root(base):
            print(
                f"warning: git cannot describe {base}, falling back to a filesystem walk",
                file=sys.stderr,
            )
            # A walk reports what is on disk, so it carries the untracked files already.
            # It applies no ignore rules, which is why it is reserved for having no other answer.
            candidates = walk_paths(base)
        else:
            candidates = (candidates or []) + [base / name for name in untracked_paths(base)]
        # The path named is itself inside a generated tree, so that tree was asked for.
        asked_inside_generated = not GENERATED_TREES.isdisjoint(Path(repo_key(base, anchor)).parts)
        for q in candidates:
            key = repo_key(q, anchor)
            if GENERATED_TREES.isdisjoint(Path(key).parts) or asked_inside_generated:
                found.append((q, key))
    keep = [
        q for q, key in found if not any(x in key for x in excludes) and q.is_file() and is_text(q)
    ]
    return sorted(set(keep))


# A non-ASCII character is typography in one place and meaning in another, so it is read by tier.
# Escapes, never literals: this file is scanned by the rule it implements.
#
# Tier 1 carries no meaning its ASCII form loses, so it always flags.
TIER1 = {
    "\u2014": "restructure",
    "\u2013": "restructure",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u2022": "-",
    "\u00a0": " ",
    "\u2011": "-",
    "\u2192": "->",
    "\u21d2": "=>",
}

# Tier 2 is an operator, and only its use between two words is a finding.
TIER2 = {
    "\u2264": "<=",
    "\u2265": ">=",
    "\u2260": "!=",
    "\u00b1": "+/-",
    "\u2212": "-",
    "\u00d7": "x",
    "\u00f7": "/",
    "\u00b7": ".",
}

# Tier 3 is a unit symbol whose ASCII form would be a lie, so it never flags.
TIER3 = frozenset(
    {
        "\u00b5",
        "\u00b0",
        "\u2126",
        "\u03c0",
        "\u00b2",
        "\u00b3",
        "\u00a7",
    }
)

# A digit, unit, or operator on either side makes a tier-2 character the range it describes.
NUMERIC = re.compile(r"[0-9]")

# The rule bans the construction, not a detectable subset, so a prose semicolon flags by default.
# A pronoun-keyed pattern found 170 of 493 and missed every imperative splice.
SEMICOLON = re.compile(r";")

# A spaced hyphen, the em-dash-style clause break and the paired aside alike.
# A compound word carries no spaces, a list marker nothing before it, and a range is digit-bounded.
DASH = re.compile(r"(?<=[^\s\d])\s+-\s+(?=[^\s\d])")

# `- **Label** - explanation` is a definition separator, structurally a colon.
# Flagging it would restructure the document format rather than the prose.
# The first dash on such a line is skipped, and any later one still counts.
# An ordered marker introduces the same construct, so `1. **Label** - ...` is one too.
LABEL_DASH = re.compile(r"^\s*(?:[-*]|[0-9]+\.)\s+\*\*[^*]+\*\*[.:]?\s+-\s+")

# The negative lookbehind keeps a word-joining character from starting a repetition:
# "either/or or must-pair" is one phrase followed by a conjunction, not a doubled word.
DUPWORD = re.compile(r"(?<![\w/-])(\w+)\s+\1\b", re.IGNORECASE)
SENT_END = re.compile(r'[.!?:]["\')\]]?\s*$')

# US English is a repo-wide rule, and the cspell gate reads README and HISTORY only.
# A British spelling anywhere else in the tree therefore reaches main unchallenged.
# Each family generates its own inflections, since an inflected spelling is as wrong as its base.
# A hand-listed family drifts the moment one form is added without the others.
# The cross product also generates forms no stem takes, which simply never match.
# `analyses` is omitted, being the US plural of `analysis` as much as a British verb form.
# `cancelled` is omitted, being the GitHub Actions job status rather than prose.
ISE_STEMS = (
    "author",
    "custom",
    "initial",
    "maxim",
    "minim",
    "normal",
    "optim",
    "organ",
    "priorit",
    "recogn",
    "serial",
    "special",
    "standard",
    "summar",
    "synchron",
    "util",
    "visual",
)
ISE_ENDINGS = (
    ("ise", "ize"),
    ("ised", "ized"),
    ("ises", "izes"),
    ("ising", "izing"),
    ("isation", "ization"),
    ("isations", "izations"),
)
OUR_STEMS = ("behavi", "col", "fav", "flav", "hon", "lab", "neighb")
OUR_ENDINGS = ("", "s", "ed", "ing", "al", "ally", "ful", "ite", "ites")
RE_STEMS = ("cent", "fib", "lit", "met", "theat")
RE_ENDINGS = (("re", "er"), ("res", "ers"), ("red", "ered"))
# The spellings that follow no family, each with the US form that replaces it.
BRITISH_ODD = {
    "analyse": "analyze",
    "analysed": "analyzed",
    "analysing": "analyzing",
    "artefact": "artifact",
    "artefacts": "artifacts",
    "catalogue": "catalog",
    "catalogues": "catalogs",
    "catalogued": "cataloged",
    "defence": "defense",
    "defences": "defenses",
    "fulfil": "fulfill",
    "fulfils": "fulfills",
    "fulfilment": "fulfillment",
    "judgement": "judgment",
    "judgements": "judgments",
    "labelled": "labeled",
    "labelling": "labeling",
    "licence": "license",
    "licences": "licenses",
    "modelled": "modeled",
    "modelling": "modeling",
    "offence": "offense",
    "offences": "offenses",
    "practise": "practice",
    "practised": "practiced",
    "practising": "practicing",
    "programme": "program",
    "programmes": "programs",
    "signalled": "signaled",
    "signalling": "signaling",
    "travelled": "traveled",
    "travelling": "traveling",
    "whilst": "while",
}


def british_spellings() -> dict[str, str]:
    """Every banned spelling mapped to the US form that replaces it."""
    words = dict(BRITISH_ODD)
    for stem in ISE_STEMS:
        words.update({stem + gb: stem + us for gb, us in ISE_ENDINGS})
    for stem in OUR_STEMS:
        words.update({f"{stem}our{end}": f"{stem}or{end}" for end in OUR_ENDINGS})
    for stem in RE_STEMS:
        words.update({stem + gb: stem + us for gb, us in RE_ENDINGS})
    return words


BRITISH = british_spellings()
# Longest alternative first, so an inflection is read whole rather than as its base word.
BRITISH_RE = re.compile(
    r"\b(?:" + "|".join(sorted(BRITISH, key=len, reverse=True)) + r")\b", re.IGNORECASE
)


def us_form(found: str) -> str:
    """The US spelling for a match, carrying the case the source wrote it in."""
    us = BRITISH[found.lower()]
    if found.isupper():
        return us.upper()
    return us.capitalize() if found[0].isupper() else us


# Comment syntax per language, since the rule governs every comment the fleet's types carry.
# A `doc` marker opens a documentation comment, which CODESTYLE governs and may run to paragraphs.
# `raw` names the quotes whose strings embed the delimiter by doubling it.
# `quote_after` names what a quote must follow to delimit a string, empty where any quote does.
# `escape` is the character that escapes the next one.
# `escape_in` names the quotes it works inside, and `escape_out` whether it works outside one.
# `carry` names the forms that survive a newline, so a marker inside one is string content.
class Syntax(TypedDict):
    line: tuple[str, ...]
    block: tuple[tuple[str, str], ...]
    doc: tuple[str, ...]
    quotes: str
    verbatim: bool
    raw: str
    quote_after: str
    escape: str
    escape_in: str
    escape_out: bool
    carry: frozenset[str]


PLAIN: Syntax = {
    "line": (),
    "block": (),
    "doc": (),
    "quotes": "\"'",
    "verbatim": False,
    "raw": "",
    "quote_after": "",
    "escape": "\\",
    "escape_in": "\"'",
    "escape_out": False,
    "carry": frozenset(),
}
HASH: Syntax = {**PLAIN, "line": ("#",)}
# A shell single-quoted string takes no escape and cannot embed its own delimiter.
# It is neither doubling nor escaped, so `'a''b'` is two adjacent strings rather than one.
# Outside a string a backslash escapes the next character, which is how `'\''` embeds a quote.
# A heredoc runs from its label to the line that repeats it, and either quote form spans lines.
SHELL: Syntax = {
    **HASH,
    "escape_in": '"',
    "escape_out": True,
    "carry": frozenset({"quote", "label"}),
}
# A YAML block scalar is the multi-line form.
# A quote delimits a scalar only at the start of a value, so a plain scalar's apostrophe is text.
# Such a quote must also not carry, since one `don't` would blank the rest of the file.
# The dash leads `quote_after` so the set does not read as a character range.
YAML: Syntax = {
    **HASH,
    "raw": "'",
    "quote_after": "-:,[{",
    "escape_in": '"',
    "carry": frozenset({"block"}),
}
# A TOML literal string is raw the same way, while its basic string keeps the backslash escape.
TOML: Syntax = {**HASH, "raw": "'", "escape_in": '"'}
C_LIKE: Syntax = {**PLAIN, "line": ("//",), "block": (("/*", "*/"),), "doc": ("///", "/**")}
# C# alone carries the verbatim string, where a backslash is ordinary and a doubled quote escapes.
CSHARP: Syntax = {**C_LIKE, "verbatim": True, "carry": frozenset({"verbatim"})}
XML_LIKE: Syntax = {**PLAIN, "block": (("<!--", "-->"),), "quotes": '"'}
# PowerShell escapes with a backtick, and both quote forms double the delimiter to embed it.
# Its double-quoted string is therefore escaped and doubling at once.
# Both forms span lines, and the here-string (`@"` to `"@`) is the delimited one.
POWERSHELL: Syntax = {
    **PLAIN,
    "line": ("#",),
    "block": (("<#", "#>"),),
    "raw": "\"'",
    "escape": "`",
    "escape_in": '"',
    "escape_out": True,
    "carry": frozenset({"quote", "here"}),
}
# PowerShell documents two comment-based help forms, a `<# ... #>` block and a run of `#` lines.
# `comment-added` reads the block form as documentation, the way C# declares `///`.
# The `#` form is a run of ordinary comment lines and is read as the comments it is written as.
# So a change writing one carries the label, `.SYNOPSIS` being a keyword no author may delete.
# Telling the two apart needs the `.`-keyword shape, a second grammar in a rule that holds none.
# The other two comment rules keep the plain spelling, where a block comment is a comment they read.
POWERSHELL_DOC: Syntax = {**POWERSHELL, "doc": ("<#",)}
INI: Syntax = {**PLAIN, "line": ("#", ";")}
LISP_LIKE: Syntax = {**PLAIN, "line": ("#",), "quotes": '"'}
# CSS has block comments only, so a `//` in it is the scheme separator of a URL.
CSS: Syntax = {**PLAIN, "block": (("/*", "*/"),)}

SYNTAX: dict[str, Syntax] = {
    # Python, shell, and the hash-commented configs
    ".py": HASH,
    ".sh": SHELL,
    ".bash": SHELL,
    ".yml": YAML,
    ".yaml": YAML,
    ".toml": TOML,
    ".tf": HASH,
    ".gitattributes": HASH,
    ".gitignore": HASH,
    # C#, C, and C++
    ".cs": CSHARP,
    ".c": C_LIKE,
    ".cpp": C_LIKE,
    ".cc": C_LIKE,
    ".cxx": C_LIKE,
    ".h": C_LIKE,
    ".hpp": C_LIKE,
    ".jsonc": C_LIKE,
    ".json5": C_LIKE,
    ".js": C_LIKE,
    ".ts": C_LIKE,
    ".css": CSS,
    ".scss": CSS,
    # JSON carries comments in practice, which is what JSONC names.
    # VS Code tasks, launch, devcontainer, and workspace files ship them under a plain .json name.
    ".json": C_LIKE,
    ".code-workspace": C_LIKE,
    # Markup and project files
    ".md": XML_LIKE,
    ".html": XML_LIKE,
    ".xml": XML_LIKE,
    ".csproj": XML_LIKE,
    ".props": XML_LIKE,
    ".targets": XML_LIKE,
    ".slnx": XML_LIKE,
    ".resx": XML_LIKE,
    # PowerShell, INI, and EDA
    ".ps1": POWERSHELL,
    ".psm1": POWERSHELL,
    ".ini": INI,
    ".cfg": INI,
    ".conf": INI,
    ".editorconfig": INI,
    ".kicad_sch": LISP_LIKE,
    ".kicad_pcb": LISP_LIKE,
    ".kicad_mod": LISP_LIKE,
}

# Extensionless files whose name fixes the syntax.
# A Dockerfile, a makefile recipe, and a git hook all hold shell, heredocs included.
BY_NAME = {
    "dockerfile": SHELL,
    "makefile": SHELL,
    "pre-commit": SHELL,
    "gemfile": HASH,
    "caddyfile": HASH,
    ".gitattributes": HASH,
    ".editorconfig": INI,
    ".gitignore": HASH,
}

# JSON proper carries no comments, so a `//` in one is data.
NO_COMMENTS = frozenset({".lock", ".csv", ".tsv", ".txt", ".svg", ".min"})


def syntax_for(path: Path) -> Syntax | None:
    """The comment syntax for this file, or None when it carries no comments."""
    name = path.name.lower()
    if name in BY_NAME:
        return BY_NAME[name]
    suffix = path.suffix.lower()
    if suffix in NO_COMMENTS:
        return None
    if suffix in SYNTAX:
        return SYNTAX[suffix]
    return HASH if not suffix else None


class Comment(NamedTuple):
    """A comment the parser found, and whether its marker opens the line."""

    line: int
    body: str
    leading: bool


class Carried(NamedTuple):
    """A string left open at the end of a line, and what it takes to close it.

    `kind` is `quote` for an ordinary one still open, `verbatim` for the doubled-quote form,
    `here` for a PowerShell here-string, `label` for a heredoc, and `block` for a YAML block
    scalar. `text` holds the open quote or the closing token, `indent` a block scalar's parent
    column, `dedent` whether a heredoc opened with `<<-`, and `queued` the (label, dedent) pairs
    stacked behind this one on the same line.
    """

    kind: str = ""
    text: str = ""
    indent: int = 0
    dedent: bool = False
    queued: tuple[tuple[str, bool], ...] = ()


CLEAR = Carried()

# The forms whose whole line is string content, judged before the line is scanned for a marker.
WHOLE_LINE = frozenset({"here", "label", "block"})


def opens_a_string(line: str, i: int, quote_after: str) -> bool:
    """Whether the quote at `i` delimits a string rather than sitting inside a bare word.

    YAML is the case this exists for: a plain scalar's apostrophe is text, and a quote delimits
    only at the start of a value. Reading one as an opener masks the rest of the line and hides a
    real trailing comment. An empty `quote_after` means the syntax has no bare-word form, so every
    quote delimits.
    """
    if not quote_after:
        return True
    j = i - 1
    while j >= 0 and line[j].isspace():
        j -= 1
    return j < 0 or line[j] in quote_after


def strip_strings(
    line: str,
    quotes: str,
    verbatim: bool = False,
    carried: Carried = CLEAR,
    raw: str = "",
    escape: str = "\\",
    escape_in: str = "\"'",
    escape_out: bool = False,
    quote_after: str = "",
) -> tuple[str, Carried]:
    """Blank quoted spans so a comment marker inside a string is not read as one.

    Length-preserving, so an offset into the result is an offset into the line.
    Two properties are read per string as it opens, because they are independent. A **doubled**
    string embeds its delimiter by repeating it, and C# spells one with an `@` prefix while shell,
    PowerShell, YAML, and TOML have forms that always are. An **escaped** string reads one
    character as escaping the next, which is a backslash almost everywhere and a backtick in
    PowerShell. PowerShell's double-quoted string is both at once, and the C# verbatim string is
    doubled and not escaped, so neither property implies the other. Reading an escape a string
    does not have consumes its closing quote and blanks the rest of the line, and missing one it
    does have ends the string early on the escaped quote. `carried` reopens a string the line
    above left open, and the second return says what this line leaves open in turn.
    """
    out = list(line)
    quote = carried.text if carried.kind in ("quote", "verbatim") else ""
    at_verbatim = carried.kind == "verbatim"
    doubled = at_verbatim or (quote != "" and quote in raw)
    escapes = quote != "" and not at_verbatim and quote in escape_in
    escaped = False
    i = 0
    while i < len(line):
        ch = line[i]
        if escaped:
            escaped = False
            out[i] = " "
        elif quote and escapes and ch == escape:
            escaped = True
            out[i] = " "
        elif doubled:
            if ch == quote and line[i + 1 : i + 2] == quote:  # a doubled quote is one character
                out[i] = out[i + 1] = " "
                i += 2
                continue
            out[i] = " " if ch != quote else ch
            if ch == quote:
                quote, doubled, escapes, at_verbatim = "", False, False, False
        elif quote:
            out[i] = " " if ch != quote else ch
            if ch == quote:
                quote, escapes = "", False
        elif escape_out and ch == escape:  # outside a string it escapes the next character
            escaped = True
            out[i] = " "
        elif ch in quotes and opens_a_string(line, i, quote_after):
            quote = ch
            # An interpolated one is spelled either way round, so read the whole prefix.
            # Only the double-quoted form has a verbatim spelling, so a char literal is ordinary.
            start = i
            while start > 0 and line[start - 1] in "@$":
                start -= 1
            at_verbatim = verbatim and ch == '"' and "@" in line[start:i]
            doubled = at_verbatim or ch in raw
            escapes = not at_verbatim and ch in escape_in
        i += 1
    if not quote:
        return "".join(out), CLEAR
    return "".join(out), Carried("verbatim" if at_verbatim else "quote", quote)


# A shell heredoc opener, read off masked code so a `<<` inside a string does not open one.
# `<<<` is a bash here-string, which is one line, so both lookarounds exclude it.
HEREDOC = re.compile(r"(?<!<)<<(-?)(?!<)")
HEREDOC_LABEL = re.compile(r'\s*(?:"([^"\n]+)"|\'([^\'\n]+)\'|([A-Za-z_][A-Za-z0-9_]*))')

# A PowerShell here-string opens on `@"` or `@'` as the last thing on its line.
HERE_STRING = re.compile(r'@(["\'])\s*$')

# A YAML block scalar: a value that is `|` or `>`, with the optional chomping and indent indicators.
# Anchored to the `:` or the sequence dash, so a plain scalar merely ending in a pipe is not one.
BLOCK_SCALAR = re.compile(r"(?::|^\s*-)\s*[|>][+-]?\d?\s*$")

# `run:` holds a script rather than data, so its `#` lines are comments this rule governs.
# Anchored to the key position, since a key merely ending in the word (`dry run:`) is data.
SCRIPT_SCALAR = re.compile(r"^\s*(?:-\s+)?run:\s*[|>]")


def opened_string(spec: Syntax, head: str, line: str) -> Carried:
    """The multi-line string this line's code opens, for the forms this syntax carries.

    `head` is the masked code ahead of any comment, so a marker inside a string cannot open one.
    A quoted heredoc label is read off the raw line at the same offset, since masking blanks it.
    """
    if "label" in spec["carry"]:
        opens = [
            (HEREDOC_LABEL.match(line, h.end()), h.group(1) == "-") for h in HEREDOC.finditer(head)
        ]
        labels = [(next(g for g in m.groups() if g), dedent) for m, dedent in opens if m]
        if labels:
            return Carried("label", labels[0][0], 0, labels[0][1], tuple(labels[1:]))
    if "here" in spec["carry"]:
        m = HERE_STRING.search(head)
        if m:
            return Carried("here", m.group(1) + "@")
    if "block" in spec["carry"] and BLOCK_SCALAR.search(head) and not SCRIPT_SCALAR.search(head):
        return Carried("block", "", len(line) - len(line.lstrip()))
    return CLEAR


def resume_at(carry: Carried, line: str) -> tuple[Carried, int | None]:
    """Where code resumes on this line, or None while the whole line is still string content.

    A heredoc's terminator is the label on a line of its own, so that line is content too. A
    here-string gives back what follows its closer, matching how a closing quote does. A block
    scalar ends by dedent rather than by a delimiter, on a line that is ordinary code.
    """
    if carry.kind == "label":
        # `<<-` strips leading tabs from the terminator, and a plain `<<` needs it at column 0.
        # An indented line is body content under either form, so ending there resumes inside it.
        if (line.lstrip("\t") if carry.dedent else line) != carry.text:
            return carry, None
        if carry.queued:
            head, dedent = carry.queued[0]
            return Carried("label", head, 0, dedent, carry.queued[1:]), None
        return CLEAR, None
    if carry.kind == "here":
        # The closing token starts the line, so an indented one is here-string content.
        if not line.startswith(carry.text):
            return carry, None
        return CLEAR, len(carry.text)
    if not line.strip() or len(line) - len(line.lstrip()) > carry.indent:
        return carry, None  # a blank line belongs to the scalar as well
    return CLEAR, 0


# A pragma, shebang, or divider is machinery rather than prose.
NOT_PROSE = re.compile(
    r"^(!|\s*[-=#*/<>]+\s*$)|noqa|type:\s*ignore|pylint|ruff:|mypy:|shellcheck"
    r"|cSpell|markdownlint|omit from toc|prettier|eslint|SPDX|Copyright"
    r"|^v\d+(\.\d+)*$"
)

# A comment that is only a URI is a reference, not a sentence, so neither case nor wrap applies.
# It cannot be capitalized or restructured without corrupting the address it exists to carry.
# A URI inside a sentence is still prose, so the whole body has to be the address and nothing else.
# The angle brackets are matched as a pair or not at all.
# One bracket alone is a typo, and exempting it would hide the typo rather than report it.
# The scheme is case-insensitive per RFC 3986, so an uppercase one is the same reference.
BARE_URI = re.compile(r"^(?:<(?:https?|ftp)://[^>\s]+>|(?:https?|ftp)://[^>\s]+)$", re.IGNORECASE)

# Two sentences on one line, guarded against an abbreviation, an initial, or a dotted identifier.
# The initial guard anchors on a word boundary, so `J. Smith` reads as one name.
# A sentence ending in an acronym such as CI is two sentences and has to be caught.
# The second sentence may open in either case, since a lowercase opening is still a second sentence.
# An ellipsis marks an elision inside one sentence, so its closing dot is not a terminator.
# The guard is that a dot preceded by a dot never terminates.
# That reads a schematic such as `... end_of_line = lf` as the one line it is.
# Splitting such a line would break the fragment it exists to show.
# It is scoped to the dot alternative, since a `?` or `!` after an ellipsis does terminate.
# Guarding the whole class would read `Really...? Yes.` as one sentence and miss a real run-on.
RUN_ON = re.compile(
    r"(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)(?:(?<!\.)\.|[!?])\s+(?=[A-Za-z])"
)

# A step marker opening a comment is a label on the sentence that follows, not a sentence of its own.
# `# 1. Deploy the hook.` is one sentence, and reading the marker's dot as a terminator made it two.
# It is stripped before the sentence checks so both the run-on and the opening-case test see the prose.
ENUM_PREFIX = re.compile(r"^\d+[.)]\s+")

# A comment body that is one token closing on a colon is a key or a heading, not a sentence.
# `# ignore:` above a commented-out block is disabled configuration.
# Capitalizing it corrupts the key a reader uncomments, so the rule would damage the file.
# The token count carries the test, since a colon ending real prose always has words before it.
KEY_ONLY = re.compile(r"^\S+:$")

# A label opening a definition names the thing being defined, so it is not the sentence's first word.
# `#   publish - 'true' when ...` documents an output named `publish`.
# Capitalizing it renames the output the workflow declares.
# This is the comment spelling of the `- **Label** - text` construct LABEL_DASH exempts in Markdown.
# It is tested where a line opens a definition, never where one continues a wrapped sentence.
# A continuation whose first word is followed by a spaced dash is a parenthetical instead.
# That is the construction the dash rule exists to catch, so exempting it would hide the violation.
# Both live instances in the tree are continuations, which is what scoped this to the case branch.
COMMENT_LABEL = re.compile(r"^[A-Za-z_][\w.-]*\s+-\s+")
CODE_FENCE = re.compile(r"^\s*(```|~~~)")

# A directive a tool reads, exempt because deleting one changes what the file does.
# Anchored at the body's start, since a comment that merely names one of these is prose.
# Read by `comment-added` alone rather than added to `NOT_PROSE`, which two gating rules share.
# Widening that pattern stopped those two reporting any comment naming a word in it.
# That is a gate narrowing where the intent was an exemption in a third rule.
TOOL_DIRECTIVE = re.compile(
    r"^(?:syntax=|escape=|fmt:\s*(?:on|off|skip)\b|pragma:|nosec\b|checkov:|hadolint\b"
    r"|renovate:|yaml-language-server:|-\*-\s*coding[:=]|pyright:|isort:|nopep8\b"
    r"|codespell:|doctest:)"
)

# The pull request label that stands `comment-added` down.
# The workflow and `repo-config/labels.json` spell it out separately, which a case asserts.
COMMENT_LABEL_NAME = "comments"

# The environment form of the same override, read here rather than passed by each caller.
# A hook entry is often one command string with nowhere to put a conditional flag.
# A downstream repository also runs this file straight from the hub, unedited.
# So a caller-side escape would reach only the hooks somebody edited to carry one.
COMMENT_ENV_NAME = "PROSE_ALLOW_COMMENTS"

# The spellings that read as off.
# The composite action's own input spells its off value `false`.
# A value mirroring that here stood the rule down at every commit while it was exported.
COMMENT_ENV_OFF = frozenset({"", "0", "false", "no", "off"})

# Both are correct English. `the the` is always a typo, so it is not here.
DUP_ALLOW = frozenset({"that that", "had had"})


def strip_inline_code(s: str) -> str:
    return re.sub(r"`[^`]*`", "``", s)


def strip_quoted(s: str) -> str:
    """Blank double-quoted spans, which hold a quotation rather than agent-authored prose.

    A rule that states its own counter-example quotes the construction it bans, so scanning the
    quotation reports the doc that documents the rule. Markdown only: in data and code files a
    double quote is structural, and blanking those spans would hide the prose inside them.
    A terminator ending the quotation survives outside the blank, since US English closes a
    sentence inside the quotes and blanking it merged that sentence into its neighbor.
    """

    def blank(m: re.Match[str]) -> str:
        inner = m.group(1)
        return '""' + (inner[-1] if inner and inner[-1] in ".!?" else "")

    return re.sub(r'"([^"\n]*)"', blank, s)


# A bullet's `**Label**:` opens the text the same way `- **Label** -` does.
# Its colon introduces the bullet rather than a list, and reading it as one excused the splice.
# The colon is written inside the emphasis as often as outside it, and both spell one construct.
# Matching `**Label**:` alone left `**Label:**` announcing a list it never announced.
LABEL_COLON = re.compile(r"^\s*(?:[-*]|[0-9]+\.)\s+\*\*[^*]+?(?:\*\*\s*:|:\s*\*\*)")

# A sentence boundary inside one line, so a list exemption is scoped to the sentence holding it.
# The guards are the run-on rule's, so an initial or an abbreviation ends nothing.
# The trailing class is the emphasis or bracket a Markdown sentence closes inside.
# Reading a bare `. ` instead left `.**` and `.)` joining a bullet's every sentence into one span.
SENTENCE_BREAK = re.compile(
    r"(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)"
    r'[.!?][*_`"\')\]]*\s+'
)

# The word cap one Markdown prose sentence may reach, the first structural house-style check.
# ASD-STE100's descriptive cap is the one adopted, since no pattern tells a procedure step apart.
# Opt-in like sentence-split, because the corpus predates the cap and a default gate would fail it.
SENTENCE_WORD_CAP = 25


def sentences(span: str) -> list[str]:
    """The span split at its sentence boundaries, empty pieces dropped.

    A list lives inside one sentence, so the sentence is the unit an exemption may be judged on.
    Judged over a whole bullet instead, a colon anywhere before the first semicolon marked the
    bullet a list and exempted every semicolon after it, however plainly one joined two clauses.
    The colon and the semicolon did not have to be near each other, or related at all.
    """
    return [s for s in SENTENCE_BREAK.split(span) if s.strip()]


def list_spans(s: str) -> list[str]:
    """Split a line into the spans that each hold their own list.

    A Markdown table row is a record of fields rather than one sentence, so judging the row whole
    let a comma in one column excuse a semicolon in another.
    """
    cells = s.strip().strip("|").split("|") if s.lstrip().startswith("|") else [s]
    return [LABEL_COLON.sub("", cell) for cell in cells]


def in_numeric_context(line: str, pos: int) -> bool:
    """Whether the character at `pos` sits in an expression rather than in a sentence.

    The discriminator is what flanks it once spaces are skipped. A digit, a tier-3 unit, or
    another operator on either side makes it the range it describes. A word on both sides makes
    it prose, which is the case the ASCII form is for.
    """

    def neighbor(step: int) -> str:
        j = pos + step
        while 0 <= j < len(line) and line[j].isspace():
            j += step
        return line[j] if 0 <= j < len(line) else ""

    return any(
        c and (NUMERIC.match(c) or c in TIER3 or c in TIER2) for c in (neighbor(-1), neighbor(1))
    )


def charset_findings(lineno: int, line: str) -> list[tuple[int, str, str]]:
    """Every non-ASCII character on the line, judged against its tier.

    An unrecognized character is reported rather than passed. A gate that allows whatever it does
    not recognize stops gating as the character set grows.
    """
    out: list[tuple[int, str, str]] = []
    for pos, ch in enumerate(line):
        if ch.isascii():
            continue
        name = unicodedata.name(ch, f"U+{ord(ch):04X}")
        if ch in TIER3:
            continue
        if ch in TIER1:
            fix = TIER1[ch]
            hint = "restructure the sentence" if fix == "restructure" else f"use '{fix}'"
            out.append((lineno, "charset", f"{name} (U+{ord(ch):04X}) -> {hint}"))
        elif ch in TIER2:
            if not in_numeric_context(line, pos):
                out.append(
                    (lineno, "charset", f"{name} (U+{ord(ch):04X}) in prose -> use '{TIER2[ch]}'")
                )
        else:
            out.append(
                (
                    lineno,
                    "charset-unknown",
                    f"{name} (U+{ord(ch):04X}) is in no tier - classify it in GOVERNANCE.md",
                )
            )
    return out


def python_comments(raw: str) -> list[Comment] | None:
    """Every comment in Python source, or None if it will not parse.

    `tokenize` rather than a regex because a `#` inside a string literal is not a comment, and a
    trailing comment is one a line-anchored pattern never sees.
    """
    out: list[Comment] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline):
            if tok.type == tokenize.COMMENT:
                col = tok.start[1]
                leading = not tok.line[:col].strip()
                body = tok.string.lstrip("#").strip()
                out.append(Comment(tok.start[0], body, leading))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return None
    return out


def extracted_comments(path: Path, lines: list[str], spec: Syntax | None = None) -> list[Comment]:
    """Every comment in the file as (line, text, starts-the-line), for any syntax the fleet uses.

    A marker inside a string literal is not a comment, so each line is scanned with quoted spans
    blanked first. A documentation comment is skipped: CODESTYLE governs those and permits the
    paragraphs this rule forbids.

    `spec` overrides what the path resolves to, which is how one rule reads a file's comments by a
    definition of its own without changing what every other rule sees.
    """
    if spec is None:
        spec = syntax_for(path)
    if spec is None:
        return []
    out: list[Comment] = []
    closing = ""
    doc_closing = ""
    carry = CLEAR
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\r")
        pos = 0
        # Every code span on the line, comments blanked, for a string this line opens.
        # Built up rather than captured once, since code after a closed block comment is code.
        code = [" "] * len(line)
        if carry.kind in WHOLE_LINE:  # the line is string content until it closes
            carry, resumed = resume_at(carry, line)
            if resumed is None:
                continue
            pos = resumed
        if doc_closing:  # CODESTYLE owns every line until it closes
            end = line.find(doc_closing)
            if end < 0:
                continue
            pos, doc_closing = end + len(doc_closing), ""
        elif closing:  # carried in from an unclosed block
            end = line.find(closing)
            body = (line if end < 0 else line[:end]).strip()
            # Only `/* */` continues a line with a leading `*`, and only on a line it continues.
            # Taking it off anywhere else edits the prose the rules then judge.
            # The marker is one `*` against whitespace, so `**bold**` and `*emphasis*` keep theirs.
            if closing == "*/" and body.startswith("*") and body[1:2].isspace():
                body = body[1:].strip()
            if body:
                out.append(Comment(n, body, True))
            if end < 0:
                continue
            pos, closing = end + len(closing), ""
        # Scan left to right and take whichever marker comes first.
        # A ceiling can only describe the first comment, so a later one was unreachable.
        while pos < len(line):
            # Mask from here rather than once per line, so comment text never sets string state.
            # A quote in a comment is prose, and reading it as a string blanks the markers after it.
            tail, tail_state = strip_strings(
                line[pos:],
                spec["quotes"],
                spec["verbatim"],
                carry,
                spec["raw"],
                spec["escape"],
                spec["escape_in"],
                spec["escape_out"],
                spec["quote_after"],
            )
            masked = " " * pos + tail
            found: str | tuple[str, str] | None = None
            at = len(line)
            for marker in spec["line"]:
                where = masked.find(marker, pos)
                if 0 <= where < at:
                    at, found = where, marker
            for opener, closer in spec["block"]:
                where = masked.find(opener, pos)
                if 0 <= where < at:
                    at, found = where, (opener, closer)
            code[pos:at] = masked[pos:at]
            if found is None:
                carry = tail_state  # the rest of the line is code
                break
            # Only the code before the marker advances the string state.
            _, carry = strip_strings(
                line[pos:at],
                spec["quotes"],
                spec["verbatim"],
                carry,
                spec["raw"],
                spec["escape"],
                spec["escape_in"],
                spec["escape_out"],
                spec["quote_after"],
            )
            # CODESTYLE owns a documentation comment, so this rule skips over it.
            # A line one runs to end of line, while a closed block one gives the rest back.
            if any(line[at:].startswith(d) for d in spec["doc"]):
                if isinstance(found, str):
                    break
                end = line.find(found[1], at + len(found[0]))
                if end < 0:
                    doc_closing = found[1]  # it carries on into the lines below
                    break
                pos = end + len(found[1])
                continue
            leading = not line[:at].strip()
            if isinstance(found, str):  # a line comment runs to end of line
                body = line[at + len(found) :].strip()
                if body:
                    out.append(Comment(n, body, leading))
                break
            opener, closer = found
            end = line.find(closer, at + len(opener))  # a quote in the comment is prose
            body = (line[at + len(opener) : end if end >= 0 else None]).strip()
            if body:
                out.append(Comment(n, body, leading))
            if end < 0:
                closing = closer
                break
            pos = end + len(closer)
        # A string opened here carries into the lines below only where the syntax has that form.
        # A block comment left open owns them instead, so nothing opens under one.
        form = CLEAR if (closing or doc_closing) else opened_string(spec, "".join(code), line)
        carry = form if form.kind else (carry if carry.kind in spec["carry"] else CLEAR)
    return out


def fenced_lines(lines: list[str]) -> set[int]:
    """Line numbers inside a fenced block, which every rule skips.

    A fenced example is quoted code rather than this file's own prose, so a comment in one belongs
    to whatever is being shown.
    """
    out: set[int] = set()
    in_fence = False
    for n, raw in enumerate(lines, 1):
        if CODE_FENCE.match(raw.rstrip("\r")):
            in_fence = not in_fence
            out.add(n)
            continue
        if in_fence:
            out.add(n)
    return out


def comment_wrap_findings(path: Path, raw: str, lines: list[str]) -> list[tuple[int, str, str]]:
    """Comment lines whose sentence wraps into the next, or that carry two sentences.

    The rule is one sentence per comment line. A wrapped sentence is the common failure, and a
    run-on is the other half of the same rule, so both are reported.
    """
    comments = python_comments(raw) if path.suffix == ".py" else None
    if comments is None:
        comments = extracted_comments(path, lines)
    skip = fenced_lines(lines)
    comments = [c for c in comments if c[0] not in skip]

    out: list[tuple[int, str, str]] = []
    prev_body = ""
    prev_no = 0
    for n, body, leading in comments:
        if (
            not body
            or NOT_PROSE.search(body)
            or BARE_URI.match(body.strip())
            or KEY_ONLY.match(body)
        ):
            prev_body = ""
            continue
        # An unpunctuated Markdown HTML comment is a structural marker, not commentary.
        # It is a label, so it takes neither a capital nor a sentence split.
        # A tool matches each one verbatim, so rewriting it breaks whatever reads it.
        # Group headers, the ToC-omit directive, and the agent-safety markers are the cases.
        # A comment that does punctuate a sentence is prose and is judged as prose.
        if path.suffix == ".md" and not SENT_END.search(body):
            prev_body = ""
            continue
        body = ENUM_PREFIX.sub("", body)
        if RUN_ON.search(strip_inline_code(body)):
            out.append((n, "comment-wrap", "two sentences on one comment line -> split them"))
        # A continuation is the very next line: two comments with code between them are separate.
        adjacent = n == prev_no + 1
        continuation = (
            adjacent
            and leading
            and prev_body
            and not SENT_END.search(prev_body)
            and body[:1].islower()
        )
        if continuation:
            out.append(
                (
                    prev_no,
                    "comment-wrap",
                    "comment sentence wraps into the next line -> one sentence per line",
                )
            )
        # A lowercase opening that is not a continuation is a sentence that failed to start.
        # A label opening a definition is exempt, since the lowercase word is the name being defined.
        elif leading and body[:1].islower() and not COMMENT_LABEL.match(body):
            out.append(
                (
                    n,
                    "comment-case",
                    (
                        "comment sentence opens in lowercase -> capitalize, or restructure so it "
                        "does not open on a lowercase name"
                    ),
                )
            )
        # A trailing comment can start a sentence the next full-line comment continues.
        # The continuation has to be a full-line comment, since a trailing one annotates its own.
        prev_body = body
        prev_no = n
    return out


def is_tool_directive(body: str) -> bool:
    """Whether a comment body is an instruction to a tool rather than a sentence.

    The name has to open the body, since a comment naming a tool mid-sentence is prose about it.
    And the body must not close as a sentence does, since anchoring the opening alone let a
    directive's own name open one and exempt the whole comment with it: `# nosec The value is read
    once.` is prose whose first word happens to be a directive name. What follows a real directive
    is an argument, and an argument does not end in a full stop.

    A directive carrying a written reason, which `checkov:skip=` and `nosec` both allow, is exempt
    while that reason is written as an argument and reported once it is punctuated as a sentence,
    which is the same test applied to the same body. So `# nosec B608 - the query is parameterized`
    is exempt and the period-terminated spelling of it is not. That is the rule's cost landing on a
    real case rather than a defect, and the label is its remedy. Recognizing the reason-carrying
    forms instead means encoding two tools' grammars here, which was tried and got four of them
    wrong in one commit: an id that is not `CKV`-prefixed, a space after `skip=`, bandit's `nosec:`
    spelling, and its test-name form.
    """
    return bool(TOOL_DIRECTIVE.match(body)) and not SENT_END.search(body)


def is_comment_prose(body: str) -> bool:
    """Whether a comment body is prose a reader judges rather than an instruction a tool reads."""
    return bool(
        body
        and not NOT_PROSE.search(body)
        and not is_tool_directive(body)
        and not BARE_URI.match(body)
        and not KEY_ONLY.match(body)
    )


def comment_bodies(path: Path, raw: str, spec: Syntax | None = None) -> list[Comment] | None:
    """The file's comments, or None where none can be read.

    A `.py` whose source does not tokenize returns None rather than falling back to the
    hash-anchored scan. That scan carries an open single-line string across the line, and a
    triple-quoted one it does not, so it reads a `#` inside a docstring as a comment.
    """
    if path.suffix.lower() == ".py":
        comments = python_comments(raw)
        if comments is None:
            return None
    else:
        comments = extracted_comments(path, raw.split("\n"), spec)
    return comments


def comment_added_findings(path: Path, raw: str) -> list[tuple[int, str, str]]:
    """Every prose comment line the file holds, which the diff scope narrows to the ones in a change.

    The whole file is read and `main` keeps only the lines the diff touches, which is the path every
    other rule already takes. Read without a diff the rule would report the tree's every comment, so
    `main` stands it down there instead.

    `git diff --unified=0` counts a modified line as an added one, so a change that edits the code on
    a line carrying a trailing comment, re-indents a commented block, or rewords a comment reports
    that comment. That is the rule's cost rather than a defect in it, and the label is the answer: an
    earlier shape read the file at the diff's base to tell those apart, and four review passes spent
    on the rename, duplicate, and prose-filter cases it opened bought precision this rule does not
    need, since the remedy for a wanted comment is the same label either way.

    Markdown is out of scope. Its prose is the document rather than a comment on one, and its HTML
    comments are structural markers a tool matches verbatim. A PowerShell `<# ... #>` block is out
    of scope the way a C# `///` comment is, both being the language's documentation form rather
    than a remark on code. It is read out by `POWERSHELL_DOC`, which is the syntax the parser already implements rather
    than a scan of this rule's own. Two such scans were written and each missed a marker inside a
    string, one standing the rule down to end of file and one to the next block's terminator.

    A bare URI and a key are not prose, and a tool directive is an instruction rather than prose,
    since deleting a `# noqa` or a `# syntax=` line changes what the file does. `NOT_PROSE` and
    `TOOL_DIRECTIVE` are the forms that are known. A directive neither names is reported, and the
    label is the remedy, since a directive whose written reason is punctuated as a sentence reads
    as one, and `TOOL_DIRECTIVE` cannot take it without encoding that tool's grammar.
    """
    if path.suffix.lower() == ".md" or syntax_for(path) is None:
        return []
    spec = POWERSHELL_DOC if path.suffix.lower() in {".ps1", ".psm1"} else None
    bodies = comment_bodies(path, raw, spec)
    if bodies is None:
        return []
    # A comment whose marker opens the line, and no other.
    # Which mid-line marker opens a comment is a parser fact that differs by language.
    # TOML and HCL carry a `#` inside a multi-line string.
    # A git pattern file holds no trailing comment at all.
    # PowerShell opens one after almost anything, and shell after its metacharacters.
    # Six attempts at deciding it from the marker's neighbourhood each got a language wrong.
    # A marker at the start of its line is unambiguous in every one of them.
    # `comment-wrap` and `comment-case` have judged only leading comments for years.
    # That is why neither ever met any of those defects.
    bodies = [c for c in bodies if c.leading]
    # One finding per line rather than one per comment, since the rule is about the line.
    # A line can carry two comments, and reporting it twice counts one line as two violations.
    seen: set[int] = set()
    out: list[tuple[int, str, str]] = []
    for comment in bodies:
        n = comment.line
        if n in seen or not is_comment_prose(comment.body):
            continue
        seen.add(n)
        out.append(
            (
                n,
                "comment-added",
                (
                    "a comment line this change adds or edits -> delete it, or allow it: the "
                    f"{COMMENT_LABEL_NAME!r} label on the pull request, {COMMENT_ENV_NAME} at a "
                    "commit, --allow-comments on a run by hand"
                ),
            )
        )
    return out


def python_docstring_lines(raw: str) -> set[int]:
    """Every source line a module, class, or function docstring occupies, empty where it will not parse.

    `ast` rather than a scan for a triple quote, since a triple-quoted string is a docstring only
    where it opens a body, and a docstring's own text may hold one.

    The lines are returned rather than the text, so a reference is reported on the line that
    carries it. A docstring's value has its escapes resolved, which no longer maps to a line.
    """
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError):
        return set()
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        first = node.body[0].value
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


def issue_ref_findings(
    path: Path, raw: str, lines: list[str], root: Path | None
) -> list[tuple[int, str, str]]:
    """Every issue or pull request reference on a surface the ban reaches.

    Instruction text is read whole, and every other Markdown file is left alone, since a tracker
    and a history exist to carry exactly these references.

    Elsewhere the comments and the Python docstrings are read, and the code between them is not. A
    reference in a string literal is fixture data as often as it is prose: a test asserting on a
    handoff's own chain builds the numbers it asserts against, and reading them would report the
    fixture rather than a claim about this repository.
    """
    out: list[tuple[int, str, str]] = []
    spans: dict[int, list[str]] = {}
    if path.suffix.lower() == ".md":
        if not is_instruction_text(path, root):
            return out
        fenced = fenced_lines(lines)
        for n, line in enumerate(lines, 1):
            if n not in fenced:
                spans.setdefault(n, []).append(line)
    else:
        for comment in comment_bodies(path, raw) or ():
            spans.setdefault(comment.line, []).append(comment.body)
        if path.suffix.lower() == ".py":
            for n in python_docstring_lines(raw):
                if n <= len(lines):
                    spans.setdefault(n, []).append(lines[n - 1])
    for n in sorted(spans):
        for text in spans[n]:
            for m in ISSUE_REF.finditer(text):
                out.append(
                    (
                        n,
                        "issue-ref",
                        (
                            f"issue or pull request reference {m.group(0)!r} -> "
                            "state the constraint it stands for, or drop the clause"
                        ),
                    )
                )
    return out


def check_file(path: Path, rules: set[str], root: Path | None = None) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    try:
        raw = path.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError):
        return out
    lines = raw.split("\n")
    # Only Markdown describes what the repo holds, so only Markdown is judged for dead paths.
    # The file's own directory anchors a relative link, the repository root anchors the rest.
    # No git means no deletion history to key on, so the rule stands down rather than guess.
    dead_root: Path | None = None
    if "dead-path" in rules and path.suffix == ".md":
        found = str(root) if root else repo_root(path)
        if found:
            dead_root = Path(found)
    if {"comment-wrap", "comment-case"} & rules:
        out.extend(f for f in comment_wrap_findings(path, raw, lines) if f[1] in rules)
    if "comment-added" in rules:
        out.extend(comment_added_findings(path, raw))
    if "issue-ref" in rules:
        out.extend(issue_ref_findings(path, raw, lines, root))
    # Outside Markdown the prose lives in the comments, and both rules judge prose, not code.
    # A source line holds identifiers and literals, and an attribute value may legally repeat.
    # Reading it rejects correct work, `class="gallery gallery-cols-1"` being the reported case.
    comments: dict[int, list[str]] = {}
    if {"spelling", "dupword"} & rules and path.suffix != ".md":
        for ln, text, _leading in extracted_comments(path, lines):
            comments.setdefault(ln, []).append(text)
    in_fence = False
    prev_txt = ""
    prev_no = 0
    for i, line in enumerate(lines, 1):
        line = line.rstrip("\r")
        # Judged before the fence and inline-code handling below, deliberately.
        # A path pasted inside a fenced transcript is the same exposure as one in a sentence.
        if "home-path" in rules and not is_operations_runbook(path, root):
            out.extend(home_path_findings(i, line))
        if CODE_FENCE.match(line):
            in_fence = not in_fence
            prev_txt = ""
            continue
        if in_fence:
            continue

        if "charset" in rules or "charset-unknown" in rules:
            out.extend(f for f in charset_findings(i, line) if f[1] in rules)

        if dead_root is not None:
            out.extend(dead_path_findings(dead_root, path.resolve().parent, i, line))

        txt = strip_inline_code(line)
        prose = strip_quoted(txt) if path.suffix == ".md" else txt

        # Both prose rules are Markdown-only until a comment can be told from code.
        # A shell script carries 78 statement separators that are not prose at all.
        if path.suffix == ".md":
            if "semicolon" in rules:
                for span in list_spans(prose):
                    # The sentence is the unit, since the list an exemption protects lives in one.
                    # Judged over a whole bullet, one colon exempted every semicolon after it.
                    for sentence in sentences(span):
                        # A list keeps its semicolons, announced by a colon or a second separator.
                        # The comma qualifies the list rather than one separator's position.
                        # An enumeration whose commas fall in a later item keeps every semicolon.
                        # Read positionally, it split one series and flagged that series' openers.
                        listish = sentence.count(";") > 1 or ":" in sentence.split(";")[0]
                        if listish and "," in sentence:
                            continue
                        for _ in SEMICOLON.finditer(sentence):
                            out.append(
                                (i, "semicolon", "semicolon in prose -> a comma or two sentences")
                            )
            if "dash" in rules:
                skip = LABEL_DASH.match(prose)
                for m in DASH.finditer(prose):
                    if skip and m.start() < skip.end():
                        continue
                    out.append(
                        (i, "dash", "spaced hyphen -> a comma, two sentences, or parentheses")
                    )

        if "dupword" in rules:
            # Each comment on the line is judged on its own rather than joined with its neighbors.
            # Joining them would read the second's opening word as a repeat of the first's last.
            texts = (
                [prose]
                if path.suffix == ".md"
                else [strip_inline_code(c) for c in comments.get(i, [])]
            )
            for text in texts:
                for m in DUPWORD.finditer(text):
                    if m.group(0).lower() in DUP_ALLOW:
                        continue
                    out.append((i, "dupword", f"duplicated word '{m.group(1)}'"))

        if "spelling" in rules:
            texts = [prose] if path.suffix == ".md" else comments.get(i, [])
            for m in BRITISH_RE.finditer(strip_inline_code(" ".join(texts))):
                found = m.group(0)
                out.append((i, "spelling", f"British spelling '{found}' -> '{us_form(found)}'"))

        if "sentence-length" in rules and path.suffix == ".md":
            span = prose.strip()
            # A table row, a heading, a link definition, and a blockquote are not prose sentences.
            structural = (
                not span or span.startswith(("|", ">", "#")) or re.match(r"^\s*\[[^\]]+\]:", span)
            )
            if not structural:
                # Counted per line, so a wrapped sentence is fragments the split rule owns.
                # A code span and a quotation each collapse to one token above, deliberately.
                for sentence in sentences(span):
                    words = len(sentence.split())
                    if words > SENTENCE_WORD_CAP:
                        msg = (
                            f"{words} words in one sentence -> "
                            f"sentences of {SENTENCE_WORD_CAP} words or fewer"
                        )
                        out.append((i, "sentence-length", msg))

        if "sentence-split" in rules and path.suffix == ".md":
            stripped = txt.strip()
            is_prose = (
                stripped
                and not stripped.startswith(("|", ">", "#"))
                and not re.match(r"^\s*\[[^\]]+\]:", stripped)
            )
            if prev_txt and is_prose:
                p = prev_txt.strip()
                # The previous prose line ended mid-sentence, and this line continues it.
                if (
                    p
                    and not SENT_END.search(p)
                    and not p.endswith((":", "-", "|"))
                    and stripped[0].islower()
                ):
                    out.append(
                        (
                            prev_no,
                            "sentence-split",
                            "sentence wraps across lines (one sentence per line)",
                        )
                    )
            prev_txt = txt if is_prose else ""
            prev_no = i
    return out


def report_paths_that_are_not_utf8() -> None:
    """Let a path holding a byte that is not UTF-8 print rather than ending the run.

    The git reads above decode with surrogateescape so such a path opens on disk, which leaves
    the lone surrogate in the name to reach this program's own output. Encoding it strictly
    raises at the line printing that name, part way through the scan, so every finding after
    that point is lost along with the run's own verdict, and the exit code becomes a traceback's
    rather than the gate's. Escaping it costs the reader one unreadable byte in one name.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    report_paths_that_are_not_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=["."])
    ap.add_argument("--check", action="append", dest="checks", choices=sorted(RULES))
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--summary", action="store_true")
    ap.add_argument(
        "--list-files",
        action="store_true",
        help="print the discovered file set and exit, for auditing the sweep scope",
    )
    ap.add_argument(
        "--diff",
        metavar="BASE",
        help="only report violations on lines changed vs BASE "
        "(matches the repo policy: fix as each file is next edited, not swept)",
    )
    ap.add_argument(
        "--allow-comments",
        action="store_true",
        help="stand `comment-added` down, for a change whose comment lines are wanted. "
        f"{COMMENT_ENV_NAME} set to anything but a false spelling does the same, for a caller "
        "with nowhere to pass a flag. What makes a CI run pass it is the calling "
        "workflow's own condition",
    )
    ap.add_argument(
        "--provenance",
        metavar="REF",
        help="name this copy of the gate on the verdict, as the composite action's "
        "owner/repo@ref, so a finding can be attributed to the commit that raised it",
    )
    a = ap.parse_args(argv)

    rules = set(a.checks or DEFAULT_RULES)

    # The invariant: the rule set is decided by what is scanned, and no path here reads the working directory to decide it.
    # Only a repository declares a model, so two of them refuse and anything else resolves to one anchor.
    # TestScanRootDecidesTheRuleSet carries the cases and the reason each one exists.
    scan_paths = a.paths or ["."]
    # An exclusion is a substring test, so the empty one matches every key and empties the scan while the run still exits 0.
    # That is the false clean this gate exists to refuse rather than to emit, arriving through an argument rather than through a resolution.
    # It is reachable from a blank line in a repository's exclusions file, since the composite action skips those and a reader reproducing its arguments by hand has no such step unless it is stated.
    # Blank is judged by the ASCII whitespace the action's own trim uses rather than by `str.strip`, whose wider set includes characters that survive that trim.
    # Judged the wider way this refuses a value the action does build, so a line holding only a non-breaking space would fail that repository's gate on every run instead of passing through as the inert exclusion it is.
    if any(not x.strip(ASCII_BLANK) for x in a.exclude):
        print(
            "error: --exclude was given an empty value, which matches every path and would "
            "report a whole-tree scan as clean. Drop the blank entry rather than passing it, "
            "the way the composite action skips a blank line in .github/prose-gate-excludes.",
            file=sys.stderr,
        )
        return 2
    # Anything that is not a file or a directory is refused rather than absorbed.
    # `discover` reads such an argument as `.`, so it scanned the caller's directory while the rule set anchored on the argument's parent.
    # Tested for what it is rather than for whether it exists, since a FIFO, a socket, and a device all exist and are none of the two.
    unusable = [p for p in scan_paths if not (Path(p).is_file() or Path(p).is_dir())]
    if unusable:
        # Quoted, since a path holding a space or a comma is unreadable in a bare comma-joined list.
        print(
            f"error: requested path(s) are not a file or a directory: {quoted(unusable)}. "
            "Refusing rather than falling back to the current directory, which would scan one "
            "tree and choose the rule set from another.",
            file=sys.stderr,
        )
        return 2
    git_roots = {found for found in (repo_root(Path(p)) for p in scan_paths) if found}
    if len(git_roots) > 1:
        print(
            f"error: the requested paths span more than one repository ({quoted(git_roots)}). "
            "Each declares its own workflow model, so no single rule set is correct for all of "
            "them. Run the gate once per repository.",
            file=sys.stderr,
        )
        return 2
    # A file anchors on its own parent rather than on `.`, which is where the caller stands.
    if git_roots:
        scan_root = Path(next(iter(git_roots)))
    else:
        first = Path(scan_paths[0])
        scan_root = first if first.is_dir() else first.parent

    # Both stand-downs are announced, since a rule that quietly stops running reads as a pass.
    # The override is a deliberate act on one change, so the run says which act it honored.
    if "comment-added" in rules:
        by_env = os.environ.get(COMMENT_ENV_NAME, "").strip().lower() not in COMMENT_ENV_OFF
        if a.allow_comments or by_env:
            rules.discard("comment-added")
            named = COMMENT_ENV_NAME if by_env and not a.allow_comments else "--allow-comments"
            print(
                f"note: comment-added stood down by {named}. In CI the composite action passes "
                "that flag from its own input instead, which the fleet's caller computes from the "
                f"{COMMENT_LABEL_NAME!r} label and from the run being a promotion, and it clears "
                f"{COMMENT_ENV_NAME} so a runner cannot decide one.",
                file=sys.stderr,
            )
        elif a.diff is None:
            rules.discard("comment-added")
            print(
                "note: comment-added is not checked without --diff, which is what tells an added "
                "comment apart from one the tree already held. Scope the run to a base to run it.",
                file=sys.stderr,
            )

    # Announced for the same reason the skip above is, a silent stand-down reads as a pass.
    if "dead-path" in rules and git_roots and shallow_checkout(scan_root):
        rules.discard("dead-path")
        print(
            "note: dead-path is not checked in a shallow clone, which holds no deletion "
            "history to key on. Fetch the full history to run it.",
            file=sys.stderr,
        )

    # Every input to a verdict is read from the repository scanned rather than from the process.
    # That covers the rule set above, the file set, the diff, and the keys joining the last two.
    # An earlier guard refused a scan of one repository while the process stood in another.
    # The diff was taken where the process stood, which intersected the scan to nothing.
    # Anchoring the diff on the scan root is what that guard was approximating.
    # It answers the case correctly rather than refusing it.
    # A path under no repository now fails at the diff itself, which is the honest error.
    files = discover(a.paths or ["."], tuple(a.exclude), scan_root)
    # Computed once, by the helper and the root discovery used.
    # A key derived twice is a key that can disagree with itself.
    keys = {f: repo_key(f, scan_root) for f in files}

    if a.list_files:
        for f in files:
            print(keys[f])
        return 0

    discovered = len(files)
    # Presence rather than truthiness, since an empty value is a base that failed to compute.
    # Reading it as no diff at all is the silent widening the refusal below exists to prevent.
    scope = changed_lines(a.diff, scan_root) if a.diff is not None else None
    if a.diff is not None and scope is None:
        # Widening to the whole tree answers a different question, and answers it silently.
        # A caller scoping to a change gets the backlog reported as though the change made it.
        # A CI adoption hits this first, where an unresolvable base walls off the first run.
        # Scoping to nothing instead would report a false clean, so neither default is honest.
        print(
            f"error: cannot diff against {a.diff!r}, so the run cannot be scoped to changed "
            "lines. Refusing to scan the whole tree instead, since that reports the existing "
            "backlog as though this change introduced it. Check the ref exists and that the "
            "checkout carries its history.",
            file=sys.stderr,
        )
        return 2
    if scope is not None:
        matched = [f for f in files if keys[f] in scope]
        # The floor every verdict below rests on, asserted rather than guarded.
        # Each route to a false clean so far was closed after a reviewer saw it.
        # The next is closed that way or not at all, which is what a floor covers.
        # A run that resolves a non-empty diff and matches none of its files failed to scope.
        # Zero alone is not the test.
        # A change touching only files the rules do not read matches nothing and is clean.
        # An image or a lock file is that case.
        # So the comparison is against the diff's own list of files this run could have read.
        if scope and not matched:
            unread = unread_diff_files(scope, a.paths or ["."], tuple(a.exclude), scan_root)
            if unread:
                shown = ", ".join(unread[:5]) + (" and more" if len(unread) > 5 else "")
                print(
                    f"error: the diff against {a.diff!r} names {len(unread)} readable file(s) "
                    f"this run was asked about, and the scan matched none of them: {shown}. "
                    "Refusing to report a clean run, since a gate that read nothing is "
                    "indistinguishable from a gate with nothing to read. Check that the run "
                    "starts at the repository top level and that the requested paths cover "
                    "the change.",
                    file=sys.stderr,
                )
                return 2
        files = matched

    total = 0
    bykind: dict[str, int] = {}
    byfile: dict[str, int] = {}
    for f in files:
        allowed = scope.get(keys[f]) if scope is not None else None
        for ln, kind, msg in check_file(f, rules, scan_root):
            if allowed is not None and ln not in allowed:
                continue
            total += 1
            bykind[kind] = bykind.get(kind, 0) + 1
            byfile[keys[f]] = byfile.get(keys[f], 0) + 1
            if not a.summary:
                print(f"{keys[f]}:{ln}: {kind}: {msg}")

    inscope = sum(len(scope[keys[f]]) for f in files) if scope is not None else None
    print(
        scope_note(len(files), discovered, inscope, a.diff, gate_provenance(a.provenance)),
        file=sys.stderr,
    )
    if a.summary or total:
        print(f"\n{total} violation(s) across {len(byfile)} file(s)", file=sys.stderr)
        for k, v in sorted(bykind.items(), key=lambda kv: -kv[1]):
            print(f"  {k:16} {v}", file=sys.stderr)
        if a.summary:
            for k, v in sorted(byfile.items(), key=lambda kv: -kv[1])[:15]:
                print(f"  {v:5}  {k}", file=sys.stderr)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
