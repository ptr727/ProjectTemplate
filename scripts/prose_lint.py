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
from typing import NamedTuple, TypedDict

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


PLAIN: Syntax = {'line': (), 'block': (), 'doc': (), 'quotes': '"\'', 'verbatim': False,
                 'raw': '', 'quote_after': '', 'escape': '\\', 'escape_in': '"\'',
                 'escape_out': False,
                 'carry': frozenset()}
HASH: Syntax = {**PLAIN, 'line': ('#',)}
# A shell single-quoted string takes no escape and cannot embed its own delimiter.
# It is neither doubling nor escaped, so `'a''b'` is two adjacent strings rather than one.
# Outside a string a backslash escapes the next character, which is how `'\''` embeds a quote.
# A heredoc runs from its label to the line that repeats it, and either quote form spans lines.
SHELL: Syntax = {**HASH, 'escape_in': '"', 'escape_out': True,
                 'carry': frozenset({'quote', 'label'})}
# A YAML block scalar is the multi-line form.
# A quote delimits a scalar only at the start of a value, so a plain scalar's apostrophe is text.
# Such a quote must also not carry, since one `don't` would blank the rest of the file.
YAML: Syntax = {**HASH, 'raw': "'", 'quote_after': ':-,[{', 'escape_in': '"',
                'carry': frozenset({'block'})}
# A TOML literal string is raw the same way, while its basic string keeps the backslash escape.
TOML: Syntax = {**HASH, 'raw': "'", 'escape_in': '"'}
C_LIKE: Syntax = {**PLAIN, 'line': ('//',), 'block': (('/*', '*/'),), 'doc': ('///', '/**')}
# C# alone carries the verbatim string, where a backslash is ordinary and a doubled quote escapes.
CSHARP: Syntax = {**C_LIKE, 'verbatim': True, 'carry': frozenset({'verbatim'})}
XML_LIKE: Syntax = {**PLAIN, 'block': (('<!--', '-->'),), 'quotes': '"'}
# PowerShell escapes with a backtick, and both quote forms double the delimiter to embed it.
# Its double-quoted string is therefore escaped and doubling at once.
# Both forms span lines, and the here-string (`@"` to `"@`) is the delimited one.
POWERSHELL: Syntax = {**PLAIN, 'line': ('#',), 'block': (('<#', '#>'),), 'raw': '"\'',
                      'escape': '`', 'escape_in': '"', 'escape_out': True,
                      'carry': frozenset({'quote', 'here'})}
INI: Syntax = {**PLAIN, 'line': ('#', ';')}
LISP_LIKE: Syntax = {**PLAIN, 'line': ('#',), 'quotes': '"'}
# CSS has block comments only, so a `//` in it is the scheme separator of a URL.
CSS: Syntax = {**PLAIN, 'block': (('/*', '*/'),)}

SYNTAX: dict[str, Syntax] = {
    # Python, shell, and the hash-commented configs
    '.py': HASH, '.sh': SHELL, '.bash': SHELL, '.yml': YAML, '.yaml': YAML,
    '.toml': TOML, '.tf': HASH, '.gitattributes': HASH, '.gitignore': HASH,
    # C#, C, and C++
    '.cs': CSHARP, '.c': C_LIKE, '.cpp': C_LIKE, '.cc': C_LIKE, '.cxx': C_LIKE,
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
# A Dockerfile, a makefile recipe, and a git hook all hold shell, heredocs included.
BY_NAME = {
    'dockerfile': SHELL, 'makefile': SHELL, 'pre-commit': SHELL, 'gemfile': HASH,
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


class Carried(NamedTuple):
    """A string left open at the end of a line, and what it takes to close it.

    `kind` is `quote` for an ordinary one still open, `verbatim` for the doubled-quote form,
    `here` for a PowerShell here-string, `label` for a heredoc, and `block` for a YAML block
    scalar. `text` holds the open quote or the closing token, `indent` a block scalar's parent
    column, `dedent` whether a heredoc opened with `<<-`, and `queued` the (label, dedent) pairs
    stacked behind this one on the same line.
    """
    kind: str = ''
    text: str = ''
    indent: int = 0
    dedent: bool = False
    queued: tuple[tuple[str, bool], ...] = ()


CLEAR = Carried()

# The forms whose whole line is string content, judged before the line is scanned for a marker.
WHOLE_LINE = frozenset({'here', 'label', 'block'})


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


def strip_strings(line: str, quotes: str, verbatim: bool = False,
                  carried: Carried = CLEAR, raw: str = '', escape: str = '\\',
                  escape_in: str = '"\'', escape_out: bool = False,
                  quote_after: str = '') -> tuple[str, Carried]:
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
    quote = carried.text if carried.kind in ('quote', 'verbatim') else ''
    at_verbatim = carried.kind == 'verbatim'
    doubled = at_verbatim or (quote != '' and quote in raw)
    escapes = quote != '' and not at_verbatim and quote in escape_in
    escaped = False
    i = 0
    while i < len(line):
        ch = line[i]
        if escaped:
            escaped = False
            out[i] = ' '
        elif quote and escapes and ch == escape:
            escaped = True
            out[i] = ' '
        elif doubled:
            if ch == quote and line[i + 1:i + 2] == quote:   # a doubled quote is one character
                out[i] = out[i + 1] = ' '
                i += 2
                continue
            out[i] = ' ' if ch != quote else ch
            if ch == quote:
                quote, doubled, escapes, at_verbatim = '', False, False, False
        elif quote:
            out[i] = ' ' if ch != quote else ch
            if ch == quote:
                quote, escapes = '', False
        elif escape_out and ch == escape:         # outside a string it escapes the next character
            escaped = True
            out[i] = ' '
        elif ch in quotes and opens_a_string(line, i, quote_after):
            quote = ch
            # An interpolated one is spelled either way round, so read the whole prefix.
            # Only the double-quoted form has a verbatim spelling, so a char literal is ordinary.
            start = i
            while start > 0 and line[start - 1] in '@$':
                start -= 1
            at_verbatim = verbatim and ch == '"' and '@' in line[start:i]
            doubled = at_verbatim or ch in raw
            escapes = not at_verbatim and ch in escape_in
        i += 1
    if not quote:
        return ''.join(out), CLEAR
    return ''.join(out), Carried('verbatim' if at_verbatim else 'quote', quote)


# A shell heredoc opener, read off masked code so a `<<` inside a string does not open one.
# `<<<` is a bash here-string, which is one line, so both lookarounds exclude it.
HEREDOC = re.compile(r'(?<!<)<<(-?)(?!<)')
HEREDOC_LABEL = re.compile(r'\s*(?:"([^"\n]+)"|\'([^\'\n]+)\'|([A-Za-z_][A-Za-z0-9_]*))')

# A PowerShell here-string opens on `@"` or `@'` as the last thing on its line.
HERE_STRING = re.compile(r'@(["\'])\s*$')

# A YAML block scalar: a value that is `|` or `>`, with the optional chomping and indent indicators.
# Anchored to the `:` or the sequence dash, so a plain scalar merely ending in a pipe is not one.
BLOCK_SCALAR = re.compile(r'(?::|^\s*-)\s*[|>][+-]?\d?\s*$')

# `run:` holds a script rather than data, so its `#` lines are comments this rule governs.
# Anchored to the key position, since a key merely ending in the word (`dry run:`) is data.
SCRIPT_SCALAR = re.compile(r'^\s*(?:-\s+)?run:\s*[|>]')


def opened_string(spec: Syntax, head: str, line: str) -> Carried:
    """The multi-line string this line's code opens, for the forms this syntax carries.

    `head` is the masked code ahead of any comment, so a marker inside a string cannot open one.
    A quoted heredoc label is read off the raw line at the same offset, since masking blanks it.
    """
    if 'label' in spec['carry']:
        opens = [(HEREDOC_LABEL.match(line, h.end()), h.group(1) == '-')
                 for h in HEREDOC.finditer(head)]
        labels = [(next(g for g in m.groups() if g), dedent) for m, dedent in opens if m]
        if labels:
            return Carried('label', labels[0][0], 0, labels[0][1], tuple(labels[1:]))
    if 'here' in spec['carry']:
        m = HERE_STRING.search(head)
        if m:
            return Carried('here', m.group(1) + '@')
    if 'block' in spec['carry'] and BLOCK_SCALAR.search(head) and not SCRIPT_SCALAR.search(head):
        return Carried('block', '', len(line) - len(line.lstrip()))
    return CLEAR


def resume_at(carry: Carried, line: str) -> tuple[Carried, int | None]:
    """Where code resumes on this line, or None while the whole line is still string content.

    A heredoc's terminator is the label on a line of its own, so that line is content too. A
    here-string gives back what follows its closer, matching how a closing quote does. A block
    scalar ends by dedent rather than by a delimiter, on a line that is ordinary code.
    """
    if carry.kind == 'label':
        # `<<-` strips leading tabs from the terminator, and a plain `<<` needs it at column 0.
        # An indented line is body content under either form, so ending there resumes inside it.
        if (line.lstrip('\t') if carry.dedent else line) != carry.text:
            return carry, None
        if carry.queued:
            head, dedent = carry.queued[0]
            return Carried('label', head, 0, dedent, carry.queued[1:]), None
        return CLEAR, None
    if carry.kind == 'here':
        # The closing token starts the line, so an indented one is here-string content.
        if not line.startswith(carry.text):
            return carry, None
        return CLEAR, len(carry.text)
    if not line.strip() or len(line) - len(line.lstrip()) > carry.indent:
        return carry, None                       # a blank line belongs to the scalar as well
    return CLEAR, 0


# A pragma, shebang, or divider is machinery rather than prose.
NOT_PROSE = re.compile(r'^(!|\s*[-=#*/<>]+\s*$)|noqa|type:\s*ignore|pylint|ruff:|mypy:|shellcheck'
                       r'|cSpell|markdownlint|omit from toc|prettier|eslint|SPDX|Copyright'
                       r'|^v\d+(\.\d+)*$')

# Two sentences on one line, guarded against an abbreviation, an initial, or a dotted identifier.
# The initial guard anchors on a word boundary, so `J. Smith` reads as one name.
# A sentence ending in an acronym such as CI is two sentences and has to be caught.
# The second sentence may open in either case, since a lowercase opening is still a second sentence.
RUN_ON = re.compile(r'(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)[.!?]\s+(?=[A-Za-z])')
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
    doc_closing = ''
    carry = CLEAR
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip('\r')
        pos = 0
        # Every code span on the line, comments blanked, for a string this line opens.
        # Built up rather than captured once, since code after a closed block comment is code.
        code = [' '] * len(line)
        if carry.kind in WHOLE_LINE:                 # the line is string content until it closes
            carry, resumed = resume_at(carry, line)
            if resumed is None:
                continue
            pos = resumed
        if doc_closing:                              # CODESTYLE owns every line until it closes
            end = line.find(doc_closing)
            if end < 0:
                continue
            pos, doc_closing = end + len(doc_closing), ''
        elif closing:                                # carried in from an unclosed block
            end = line.find(closing)
            body = (line if end < 0 else line[:end]).strip()
            # Only `/* */` continues a line with a leading `*`, and only on a line it continues.
            # Taking it off anywhere else edits the prose the rules then judge.
            # The marker is one `*` against whitespace, so `**bold**` and `*emphasis*` keep theirs.
            if closing == '*/' and body.startswith('*') and body[1:2].isspace():
                body = body[1:].strip()
            if body:
                out.append((n, body, True))
            if end < 0:
                continue
            pos, closing = end + len(closing), ''
        # Scan left to right and take whichever marker comes first.
        # A ceiling can only describe the first comment, so a later one was unreachable.
        while pos < len(line):
            # Mask from here rather than once per line, so comment text never sets string state.
            # A quote in a comment is prose, and reading it as a string blanks the markers after it.
            tail, tail_state = strip_strings(line[pos:], spec['quotes'], spec['verbatim'], carry,
                                             spec['raw'], spec['escape'], spec['escape_in'],
                                             spec['escape_out'], spec['quote_after'])
            masked = ' ' * pos + tail
            found: str | tuple[str, str] | None = None
            at = len(line)
            for marker in spec['line']:
                where = masked.find(marker, pos)
                if 0 <= where < at:
                    at, found = where, marker
            for opener, closer in spec['block']:
                where = masked.find(opener, pos)
                if 0 <= where < at:
                    at, found = where, (opener, closer)
            code[pos:at] = masked[pos:at]
            if found is None:
                carry = tail_state                   # the rest of the line is code
                break
            # Only the code before the marker advances the string state.
            _, carry = strip_strings(line[pos:at], spec['quotes'], spec['verbatim'], carry,
                                     spec['raw'], spec['escape'], spec['escape_in'],
                                     spec['escape_out'], spec['quote_after'])
            # CODESTYLE owns a documentation comment, so this rule skips over it.
            # A line one runs to end of line, while a closed block one gives the rest back.
            if any(line[at:].startswith(d) for d in spec['doc']):
                if isinstance(found, str):
                    break
                end = line.find(found[1], at + len(found[0]))
                if end < 0:
                    doc_closing = found[1]               # it carries on into the lines below
                    break
                pos = end + len(found[1])
                continue
            leading = not line[:at].strip()
            if isinstance(found, str):               # a line comment runs to end of line
                body = line[at + len(found):].strip()
                if body:
                    out.append((n, body, leading))
                break
            opener, closer = found
            end = line.find(closer, at + len(opener))    # a quote in the comment is prose
            body = (line[at + len(opener):end if end >= 0 else None]).strip()
            if body:
                out.append((n, body, leading))
            if end < 0:
                closing = closer
                break
            pos = end + len(closer)
        # A string opened here carries into the lines below only where the syntax has that form.
        # A block comment left open owns them instead, so nothing opens under one.
        form = CLEAR if (closing or doc_closing) else opened_string(spec, ''.join(code), line)
        carry = form if form.kind else (carry if carry.kind in spec['carry'] else CLEAR)
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
        # A trailing comment can start a sentence the next full-line comment continues.
        # The continuation has to be a full-line comment, since a trailing one annotates its own.
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
                    # A list keeps its semicolons, announced by a colon or a second separator.
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
