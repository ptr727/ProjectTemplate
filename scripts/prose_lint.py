#!/usr/bin/env python3
"""Enforce the GOVERNANCE.md "Documentation Style Conventions" rules no other linter checks.

markdownlint, cspell, actionlint, and editorconfig-checker all pass on prose that breaks
these rules, so nothing enforced them before this script. Rules implemented:
  ascii          Write ASCII in all agent-authored text.
  semicolon      No semicolon joining two independent clauses.
  dupword        No duplicated consecutive word.
  sentence-split A sentence must not wrap across lines (one sentence per line).

Exit 1 if any violation is found. Read-only, never edits.
"""
from __future__ import annotations
import argparse, re, subprocess, sys, unicodedata
from pathlib import Path

# One source of truth for the rule names, so the CLI choices cannot drift from what check_file
# implements. Writing them out separately is how a rule exists in one place and not another.
RULES = {
    'ascii': 'typographic Unicode where an ASCII equivalent exists',
    'semicolon': 'a semicolon joining two independent clauses',
    'dupword': 'a duplicated consecutive word',
    'sentence-split': 'a sentence wrapping across lines',
}
DEFAULT_RULES = frozenset({'ascii', 'semicolon', 'dupword'})

# Produced rather than authored trees, consulted only on the no-git fallback path.
# Where git can answer, its own ignore rules are the better answer.
GENERATED_ROOTS = frozenset({
    '.git', '.artifacts', '.mypy_cache', '.ruff_cache', '.pytest_cache',
    '.venv', '__pycache__', 'node_modules', 'bin', 'obj', 'dist',
})

# A floor on what a healthy sweep of this repo reaches, asserted by the tests.
# A sweep that quietly stops finding files satisfies every rule by having nothing to read.
LEAST_PLAUSIBLE = 60


def rel(path: Path) -> str:
    """The repo-relative posix key a git diff uses for this path.

    `removeprefix` rather than `lstrip`, which takes a character set and ate the leading dot of
    every dotfile - it turned `.github/workflows/x.yml` into `github/workflows/x.yml`, so --diff
    could never match a path under a dot directory.
    """
    return path.as_posix().removeprefix('./')


def changed_lines(base: str) -> dict[str, set[int]] | None:
    """Map path -> set of line numbers added/changed vs `base`. None if git fails."""
    try:
        d = subprocess.run(['git', 'diff', '--unified=0', '--no-color', base, '--'],
                           capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    out: dict[str, set[int]] = {}
    cur = None
    for line in d.split('\n'):
        if line.startswith('+++ b/'):
            cur = line[6:]
            out.setdefault(cur, set())
        elif line.startswith('@@') and cur:
            m = re.search(r'\+(\d+)(?:,(\d+))?', line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or 1)
                out[cur].update(range(start, start + count))
    return out


def tracked_paths(root: Path) -> list[Path] | None:
    """Paths git tracks under `root`, or None when git cannot answer.

    Empty output is a None too. `git ls-files` succeeds with no output in an initialized but
    empty checkout, and reading that as an empty file set would scan nothing and report success.
    """
    try:
        r = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'],
                           capture_output=True, text=True)
    except (OSError, ValueError):
        return None
    if r.returncode != 0 or not r.stdout.strip('\0'):
        return None
    return [root / name for name in r.stdout.split('\0') if name]


def walk_paths(root: Path) -> list[Path]:
    """Every file under `root` minus the generated trees, for a checkout git cannot describe.

    `git check-ignore` fails on exactly the machine that has no git, so this path asserts the
    generated-root rule by name instead of asking git which paths are ignored.
    """
    return [p for p in root.rglob('*')
            if p.is_file() and not GENERATED_ROOTS.intersection(p.relative_to(root).parts)]


def is_text(path: Path) -> bool:
    """A NUL byte in the first block marks a binary, the test the line-endings rule prescribes."""
    try:
        with path.open('rb') as fh:
            return b'\0' not in fh.read(8192)
    except OSError:
        return False


def discover(paths: list[str], excludes: tuple[str, ...] = ()) -> list[Path]:
    """Every authored text file the rules govern, scoped by what git tracks.

    The line-endings rule already requires a repo-wide sweep be scoped to `git ls-files` rather
    than a directory list, which covers what its author thought of and silently stops covering
    whatever is added next. An extension allowlist has that same defect, so the filter here is
    whether the file is text, not whether its suffix was thought of.

    An explicit file argument bypasses discovery, so a single file can always be checked directly.
    """
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            found.append(p)
            continue
        root = p if p.is_dir() else Path('.')
        tracked = tracked_paths(root)
        if tracked is None:
            print(f'warning: git cannot describe {root}, falling back to a filesystem walk',
                  file=sys.stderr)
            tracked = walk_paths(root)
        found.extend(tracked)
    keep = [p for p in found
            if not any(x in rel(p) for x in excludes) and p.is_file() and is_text(p)]
    return sorted(set(keep))


# Typographic Unicode the rule says to replace with its ASCII equivalent on sight.
# GOVERNANCE.md allows two narrow exceptions that are deliberately NOT flagged:
# scientific/technical symbols with no clean ASCII equivalent (ohm, micro, degree,
# pi, superscripts, section sign) and developer-typed Unicode such as emoji.
# Only substitutable typography appears here.
# Escapes, never literals - this file is scanned by the rule below, and a literal is invisible.
SUGGEST = {
    '\u2014': '-', '\u2013': '-', '\u2018': "'", '\u2019': "'",
    '\u201c': '"', '\u201d': '"', '\u2026': '...', '\u00a0': ' ',
    '\u2022': '-', '\u2011': '-', '\u2192': '->', '\u21d2': '=>',
    '\u2264': '<=', '\u2265': '>=',
}

# A semicolon splice: "<clause>; <pronoun/article/subject> <verb>..."
# Deliberately conservative - only flags a lowercase word after "; " that starts
# a clause with a following finite verb. List semicolons and code are not matched.
SPLICE = re.compile(
    r';\s+(?P<w>it|this|that|they|he|she|we|you|the|a|an|there|these|those)\s+\w+',
    re.IGNORECASE)

# The negative lookbehind keeps a word-joining character from starting a repetition:
# "either/or or must-pair" is one phrase followed by a conjunction, not a doubled word.
DUPWORD = re.compile(r'(?<![\w/-])(\w+)\s+\1\b', re.IGNORECASE)
SENT_END = re.compile(r'[.!?]["\')\]]?\s*$')
CODE_FENCE = re.compile(r'^\s*(```|~~~)')

# Both are correct English. `the the` is always a typo, so it is not here.
DUP_ALLOW = frozenset({'that that', 'had had'})


def strip_inline_code(s: str) -> str:
    return re.sub(r'`[^`]*`', '``', s)


def strip_quoted(s: str) -> str:
    """Blank double-quoted spans, which hold a quotation rather than agent-authored prose.

    A rule that states its own counter-example quotes the construction it bans, so scanning the
    quotation reports the doc that documents the rule. Markdown only: in data and code files a
    double quote is structural, and blanking those spans would hide the prose inside them.
    """
    return re.sub(r'"[^"\n]*"', '""', s)


def check_file(path: Path, rules: set[str]) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    try:
        raw = path.read_bytes().decode('utf-8')
    except (UnicodeDecodeError, OSError):
        return out
    lines = raw.split('\n')
    in_fence = False
    prev_txt = ''
    prev_no = 0
    for i, line in enumerate(lines, 1):
        line = line.rstrip('\r')
        if CODE_FENCE.match(line):
            in_fence = not in_fence
            prev_txt = ''
            continue
        if in_fence:
            continue

        if 'ascii' in rules:
            for ch in set(line):
                fix = SUGGEST.get(ch)
                if fix is None:
                    continue
                name = unicodedata.name(ch, f'U+{ord(ch):04X}')
                out.append((i, 'ascii', f"typographic {name} -> use '{fix}'"))

        txt = strip_inline_code(line)
        prose = strip_quoted(txt) if path.suffix == '.md' else txt

        if 'semicolon' in rules:
            m = SPLICE.search(prose)
            if m:
                out.append((i, 'semicolon', f"semicolon splice before '{m.group('w')}'"))

        if 'dupword' in rules:
            for m in DUPWORD.finditer(prose):
                if m.group(0).lower() in DUP_ALLOW:
                    continue
                out.append((i, 'dupword', f"duplicated word '{m.group(1)}'"))

        if 'sentence-split' in rules and path.suffix == '.md':
            stripped = txt.strip()
            is_prose = (stripped and not stripped.startswith(('|', '>', '#'))
                        and not re.match(r'^\s*\[[^\]]+\]:', stripped))
            if prev_txt and is_prose:
                p = prev_txt.strip()
                # previous prose line ended mid-sentence and this line continues it
                if (p and not SENT_END.search(p) and not p.endswith((':', '-', '|'))
                        and stripped[0].islower()):
                    out.append((prev_no, 'sentence-split',
                                'sentence wraps across lines (one sentence per line)'))
            prev_txt = txt if is_prose else ''
            prev_no = i
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='*', default=['.'])
    ap.add_argument('--check', action='append', dest='checks', choices=sorted(RULES))
    ap.add_argument('--exclude', action='append', default=[])
    ap.add_argument('--summary', action='store_true')
    ap.add_argument('--list-files', action='store_true',
                    help='print the discovered file set and exit, for auditing the sweep scope')
    ap.add_argument('--diff', metavar='BASE',
                    help='only report violations on lines changed vs BASE '
                         '(matches the repo policy: fix as each file is next edited, not swept)')
    a = ap.parse_args(argv)

    rules = set(a.checks or DEFAULT_RULES)
    files = discover(a.paths or ['.'], tuple(a.exclude))

    if a.list_files:
        for f in files:
            print(rel(f))
        return 0

    scope = changed_lines(a.diff) if a.diff else None
    if a.diff and scope is None:
        print('warning: git diff failed; falling back to whole-tree scan', file=sys.stderr)
    if scope is not None:
        files = [f for f in files if rel(f) in scope]

    total = 0
    bykind: dict[str, int] = {}
    byfile: dict[str, int] = {}
    for f in files:
        allowed = scope.get(rel(f)) if scope is not None else None
        for ln, kind, msg in check_file(f, rules):
            if allowed is not None and ln not in allowed:
                continue
            total += 1
            bykind[kind] = bykind.get(kind, 0) + 1
            byfile[rel(f)] = byfile.get(rel(f), 0) + 1
            if not a.summary:
                print(f'{rel(f)}:{ln}: {kind}: {msg}')

    if a.summary or total:
        print(f'\n{total} violation(s) across {len(byfile)} file(s)', file=sys.stderr)
        for k, v in sorted(bykind.items(), key=lambda kv: -kv[1]):
            print(f'  {k:16} {v}', file=sys.stderr)
        if a.summary:
            for k, v in sorted(byfile.items(), key=lambda kv: -kv[1])[:15]:
                print(f'  {v:5}  {k}', file=sys.stderr)
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())
