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
  dupword        No duplicated consecutive word.
  sentence-split A sentence must not wrap across lines (one sentence per line).

Exit 1 if any violation is found. Read-only, never edits.
"""
from __future__ import annotations
import argparse, io, re, subprocess, sys, tokenize, unicodedata
from pathlib import Path
from typing import TypedDict

# One source of truth for the rule names, so the CLI choices cannot drift from check_file.
RULES = {
    'charset': 'a non-ASCII character its tier does not permit here',
    'charset-unknown': 'a non-ASCII character in no tier',
    'semicolon': 'a semicolon in prose, outside a list that already carries commas',
    'dash': 'a spaced hyphen joining or interrupting a sentence',
    'comment-wrap': 'a comment sentence wrapped across lines, or two on one line',
    'comment-case': 'a comment sentence opening in lowercase',
    'dupword': 'a duplicated consecutive word',
    'sentence-split': 'a sentence wrapping across lines',
}
DEFAULT_RULES = frozenset({'charset', 'charset-unknown', 'semicolon', 'dash', 'dupword'})

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


# A non-ASCII character is typography in one place and meaning in another, so it is read by tier.
# Escapes, never literals: this file is scanned by the rule it implements.
#
# Tier 1 carries no meaning its ASCII form loses, so it always flags.
TIER1 = {
    '\u2014': 'restructure', '\u2013': 'restructure', '\u2018': "'",
    '\u2019': "'", '\u201c': '"', '\u201d': '"',
    '\u2026': '...', '\u2022': '-', '\u00a0': ' ',
    '\u2011': '-', '\u2192': '->', '\u21d2': '=>',
}

# Tier 2 is an operator, and only its use between two words is a finding.
TIER2 = {
    '\u2264': '<=', '\u2265': '>=', '\u2260': '!=',
    '\u00b1': '+/-', '\u2212': '-', '\u00d7': 'x',
    '\u00f7': '/', '\u00b7': '.',
}

# Tier 3 is a unit symbol whose ASCII form would be a lie, so it never flags.
TIER3 = frozenset({
    '\u00b5', '\u00b0', '\u2126', '\u03c0', '\u00b2', '\u00b3', '\u00a7',
})

# A digit, unit, or operator on either side makes a tier-2 character the range it describes.
NUMERIC = re.compile(r'[0-9]')

# The rule bans the construction, not a detectable subset, so a prose semicolon flags by default.
# A pronoun-keyed pattern found 170 of 493 and missed every imperative splice.
SEMICOLON = re.compile(r';')

# A spaced hyphen, the em-dash-style clause break and the paired aside alike.
# A compound word carries no spaces, a list marker nothing before it, and a range is digit-bounded.
DASH = re.compile(r'(?<=[^\s\d])\s+-\s+(?=[^\s\d])')

# `- **Label** - explanation` is a definition separator, structurally a colon.
# Flagging it would restructure the document format rather than the prose.
# The first dash on such a line is skipped, and any later one still counts.
LABEL_DASH = re.compile(r'^\s*[-*]\s+\*\*[^*]+\*\*[.:]?\s+-\s+')

# The negative lookbehind keeps a word-joining character from starting a repetition:
# "either/or or must-pair" is one phrase followed by a conjunction, not a doubled word.
DUPWORD = re.compile(r'(?<![\w/-])(\w+)\s+\1\b', re.IGNORECASE)
SENT_END = re.compile(r'[.!?:]["\')\]]?\s*$')

# Comment syntax per language, since the rule governs every comment the fleet's types carry.
# A `doc` marker opens a documentation comment, which CODESTYLE governs and may run to paragraphs.
class Syntax(TypedDict):
    line: tuple[str, ...]
    block: tuple[tuple[str, str], ...]
    doc: tuple[str, ...]
    quotes: str


HASH: Syntax = {'line': ('#',), 'block': (), 'doc': (), 'quotes': '"\''}
C_LIKE: Syntax = {'line': ('//',), 'block': (('/*', '*/'),), 'doc': ('///', '/**'), 'quotes': '"\''}
XML_LIKE: Syntax = {'line': (), 'block': (('<!--', '-->'),), 'doc': (), 'quotes': '"'}
POWERSHELL: Syntax = {'line': ('#',), 'block': (('<#', '#>'),), 'doc': (), 'quotes': '"\''}
INI: Syntax = {'line': ('#', ';'), 'block': (), 'doc': (), 'quotes': '"\''}
LISP_LIKE: Syntax = {'line': ('#',), 'block': (), 'doc': (), 'quotes': '"'}
# CSS has block comments only, so a `//` in it is the scheme separator of a URL.
CSS: Syntax = {'line': (), 'block': (('/*', '*/'),), 'doc': (), 'quotes': '"\''}

SYNTAX: dict[str, Syntax] = {
    # Python, shell, and the hash-commented configs
    '.py': HASH, '.sh': HASH, '.bash': HASH, '.yml': HASH, '.yaml': HASH,
    '.toml': HASH, '.tf': HASH, '.gitattributes': HASH, '.gitignore': HASH,
    # C#, C, and C++
    '.cs': C_LIKE, '.c': C_LIKE, '.cpp': C_LIKE, '.cc': C_LIKE, '.cxx': C_LIKE,
    '.h': C_LIKE, '.hpp': C_LIKE, '.jsonc': C_LIKE, '.json5': C_LIKE,
    '.js': C_LIKE, '.ts': C_LIKE, '.css': CSS, '.scss': CSS,
    # JSON carries comments in practice, which is what JSONC names.
    # VS Code tasks, launch, devcontainer, and workspace files ship them under a plain .json name.
    '.json': C_LIKE, '.code-workspace': C_LIKE,
    # Markup and project files
    '.md': XML_LIKE, '.html': XML_LIKE, '.xml': XML_LIKE, '.csproj': XML_LIKE,
    '.props': XML_LIKE, '.targets': XML_LIKE, '.slnx': XML_LIKE, '.resx': XML_LIKE,
    # PowerShell, INI, and EDA
    '.ps1': POWERSHELL, '.psm1': POWERSHELL,
    '.ini': INI, '.cfg': INI, '.conf': INI, '.editorconfig': INI,
    '.kicad_sch': LISP_LIKE, '.kicad_pcb': LISP_LIKE, '.kicad_mod': LISP_LIKE,
}

# Extensionless files whose name fixes the syntax.
BY_NAME = {
    'dockerfile': HASH, 'makefile': HASH, 'pre-commit': HASH, 'gemfile': HASH,
    'caddyfile': HASH, '.gitattributes': HASH, '.editorconfig': INI, '.gitignore': HASH,
}

# JSON proper carries no comments, so a `//` in one is data.
NO_COMMENTS = frozenset({'.lock', '.csv', '.tsv', '.txt', '.svg', '.min'})


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


def strip_strings(line: str, quotes: str) -> str:
    """Blank quoted spans so a comment marker inside a string is not read as one.

    Length-preserving, so an offset into the result is an offset into the line.
    """
    out = list(line)
    quote = ''
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            out[i] = ' '
            continue
        if ch == '\\' and quote:
            escaped = True
            out[i] = ' '
            continue
        if quote:
            out[i] = ' ' if ch != quote else ch
            if ch == quote:
                quote = ''
        elif ch in quotes:
            quote = ch
    return ''.join(out)


# A pragma, shebang, or divider is machinery rather than prose.
NOT_PROSE = re.compile(r'^(!|\s*[-=#*/<>]+\s*$)|noqa|type:\s*ignore|pylint|ruff:|mypy:|shellcheck'
                       r'|cSpell|markdownlint|omit from toc|prettier|eslint|SPDX|Copyright'
                       r'|^v\d+(\.\d+)*$')

# Two sentences on one line, guarded against an abbreviation or a dotted identifier.
RUN_ON = re.compile(r'(?<![A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)[.!?]\s+(?=[A-Z])')
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
        return line[j] if 0 <= j < len(line) else ''

    return any(c and (NUMERIC.match(c) or c in TIER3 or c in TIER2)
               for c in (neighbor(-1), neighbor(1)))


def charset_findings(lineno: int, line: str) -> list[tuple[int, str, str]]:
    """Every non-ASCII character on the line, judged against its tier.

    An unrecognized character is reported rather than passed. A gate that allows whatever it does
    not recognize stops gating as the character set grows.
    """
    out: list[tuple[int, str, str]] = []
    for pos, ch in enumerate(line):
        if ch.isascii():
            continue
        name = unicodedata.name(ch, f'U+{ord(ch):04X}')
        if ch in TIER3:
            continue
        if ch in TIER1:
            fix = TIER1[ch]
            hint = 'restructure the sentence' if fix == 'restructure' else f"use '{fix}'"
            out.append((lineno, 'charset', f'{name} (U+{ord(ch):04X}) -> {hint}'))
        elif ch in TIER2:
            if not in_numeric_context(line, pos):
                out.append((lineno, 'charset',
                            f"{name} (U+{ord(ch):04X}) in prose -> use '{TIER2[ch]}'"))
        else:
            out.append((lineno, 'charset-unknown',
                        f'{name} (U+{ord(ch):04X}) is in no tier - classify it in GOVERNANCE.md'))
    return out


def python_comments(raw: str) -> list[tuple[int, str, bool]] | None:
    """Every comment in Python source as (line, text, starts-the-line), or None if it will not parse.

    `tokenize` rather than a regex because a `#` inside a string literal is not a comment, and a
    trailing comment is one a line-anchored pattern never sees.
    """
    out: list[tuple[int, str, bool]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline):
            if tok.type == tokenize.COMMENT:
                leading = not tok.line[:tok.start[1]].strip()
                out.append((tok.start[0], tok.string.lstrip('#').strip(), leading))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return None
    return out


def extracted_comments(path: Path, lines: list[str]) -> list[tuple[int, str, bool]]:
    """Every comment in the file as (line, text, starts-the-line), for any syntax the fleet uses.

    A marker inside a string literal is not a comment, so each line is scanned with quoted spans
    blanked first. A documentation comment is skipped: CODESTYLE governs those and permits the
    paragraphs this rule forbids.
    """
    spec = syntax_for(path)
    if spec is None:
        return []
    out: list[tuple[int, str, bool]] = []
    closing = ''
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip('\r')
        if closing:                                  # inside a block comment
            end = line.find(closing)
            body = (line if end < 0 else line[:end]).strip().lstrip('*').strip()
            if body:
                out.append((n, body, True))
            closing = '' if end >= 0 else closing
            continue
        masked = strip_strings(line, spec['quotes'])
        cut = len(line)
        leading = True
        for opener, closer in spec['block']:
            at = masked.find(opener)
            if 0 <= at < cut:
                if any(line[at:].startswith(d) for d in spec['doc']):
                    continue
                cut, leading = at, not line[:at].strip()
                end = masked.find(closer, at + len(opener))
                body = (line[at + len(opener):end if end >= 0 else None]).strip().lstrip('*').strip()
                if body:
                    out.append((n, body, leading))
                closing = '' if end >= 0 else closer
        if closing:
            continue
        for marker in spec['line']:
            at = masked.find(marker)
            if 0 <= at < cut:
                if any(line[at:].startswith(d) for d in spec['doc']):
                    continue
                body = line[at + len(marker):].strip()
                if body:
                    out.append((n, body, not line[:at].strip()))
                cut = at
    return out


def fenced_lines(lines: list[str]) -> set[int]:
    """Line numbers inside a fenced block, which every rule skips.

    A fenced example is quoted code rather than this file's own prose, so a comment in one belongs
    to whatever is being shown.
    """
    out: set[int] = set()
    in_fence = False
    for n, raw in enumerate(lines, 1):
        if CODE_FENCE.match(raw.rstrip('\r')):
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
    comments = python_comments(raw) if path.suffix == '.py' else None
    if comments is None:
        comments = extracted_comments(path, lines)
    skip = fenced_lines(lines)
    comments = [c for c in comments if c[0] not in skip]

    out: list[tuple[int, str, str]] = []
    prev_body = ''
    prev_no = 0
    for n, body, leading in comments:
        if not body or NOT_PROSE.search(body):
            prev_body = ''
            continue
        if RUN_ON.search(strip_inline_code(body)):
            out.append((n, 'comment-wrap', 'two sentences on one comment line -> split them'))
        # A continuation is the very next line: two comments with code between them are separate.
        adjacent = n == prev_no + 1
        continuation = (adjacent and leading and prev_body
                        and not SENT_END.search(prev_body) and body[:1].islower())
        if continuation:
            out.append((prev_no, 'comment-wrap',
                        'comment sentence wraps into the next line -> one sentence per line'))
        # A lowercase opening that is not a continuation is a sentence that failed to start.
        elif leading and body[:1].islower():
            out.append((n, 'comment-case',
                        'comment sentence opens in lowercase -> capitalize, or restructure so it '
                        'does not open on a lowercase name'))
        # A trailing comment can start a sentence the next full-line comment continues, so it is
        # remembered. The continuation itself still has to be a full-line comment, since a
        # trailing one annotates its own line rather than continuing the line above.
        prev_body = body
        prev_no = n
    return out


def check_file(path: Path, rules: set[str]) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    try:
        raw = path.read_bytes().decode('utf-8')
    except (UnicodeDecodeError, OSError):
        return out
    lines = raw.split('\n')
    if {'comment-wrap', 'comment-case'} & rules:
        out.extend(f for f in comment_wrap_findings(path, raw, lines) if f[1] in rules)
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

        if 'charset' in rules or 'charset-unknown' in rules:
            out.extend(f for f in charset_findings(i, line) if f[1] in rules)

        txt = strip_inline_code(line)
        prose = strip_quoted(txt) if path.suffix == '.md' else txt

        # Both prose rules are markdown-only until a comment can be told from code.
        # A shell script carries 78 statement separators that are not prose at all.
        if path.suffix == '.md':
            if 'semicolon' in rules:
                listish = prose.count(';') > 1 or ':' in prose.split(';')[0]
                for m in SEMICOLON.finditer(prose):
                    # A list keeps its semicolons, and a list announces itself with a colon or by
                    # having more than one separator.
                    if listish and ',' in prose[:m.start()]:
                        continue
                    out.append((i, 'semicolon', 'semicolon in prose -> a comma or two sentences'))
            if 'dash' in rules:
                skip = LABEL_DASH.match(prose)
                for m in DASH.finditer(prose):
                    if skip and m.start() < skip.end():
                        continue
                    out.append((i, 'dash',
                                'spaced hyphen -> a comma, two sentences, or parentheses'))

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
                # The previous prose line ended mid-sentence, and this line continues it.
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
