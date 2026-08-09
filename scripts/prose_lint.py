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
  spelling       No British spelling, the repo-wide convention being US English.
  home-path      No absolute home path naming a real account, per the representative-data rule.

Exit 1 if any violation is found. Read-only, never edits.
"""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tokenize
import unicodedata
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
    'spelling': 'a British spelling where the repo convention is US English',
    'home-path': 'an absolute home path naming a real account',
}
DEFAULT_RULES = frozenset({'charset', 'charset-unknown', 'semicolon', 'dash', 'dupword',
                           'spelling', 'comment-wrap', 'comment-case', 'home-path'})

# Trees this repo generates rather than authors, skipped when a wider scan expands into them.
# The gate then measures hand-written prose.
# `spec/audit.py` writes `reports/`, so a finding there is the engine's phrasing, not an author's.
# No edit to that tree can fix one.
# Naming one of these paths directly still reads it, so nothing becomes uncheckable.
GENERATED_TREES = frozenset({'reports'})

# Produced rather than authored trees, consulted only on the no-git fallback path.
# Where git can answer, its own ignore rules are the better answer.
GENERATED_ROOTS = frozenset({
    '.git', '.artifacts', '.mypy_cache', '.ruff_cache', '.pytest_cache',
    '.venv', '__pycache__', 'node_modules', 'bin', 'obj', 'dist',
})

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
    r'(?:/home/|/Users/|[A-Za-z]:\\(?i:users)\\)(?P<user>[A-Za-z][A-Za-z0-9._-]*)')

# Accounts that belong to a container or a runner rather than to a person.
# Every one is a fixed name an image ships, so a path under it names no environment.
# `vscode` is the devcontainer user this repo's own snippets mount into.
# `runner` is the GitHub Actions user, and the rest are stock image accounts.
SERVICE_ACCOUNTS = frozenset({'vscode', 'runner', 'root', 'ubuntu', 'node', 'shared', 'public'})


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
        if r in ('', '.'):
            return True
        if key == r or key.startswith(r + '/'):
            return True
    return False


def unread_diff_files(scope: dict[str, set[int]], paths: list[str],
                      excludes: tuple[str, ...]) -> list[str]:
    """Files the diff names that this run was asked about and could have read, in sorted order.

    Keys are resolved against the repository top level rather than the working directory, because
    `git diff` reports repository-relative paths while discovery keys off the directory the run
    started in. Reading them against the working directory would make this list empty from a
    subdirectory, which is the one place it most needs to be full.
    """
    root = Path(repo_root(Path('.')) or '.')
    out: list[str] = []
    for key in sorted(scope):
        if not asked_about(key, paths):
            continue
        if any(x in key for x in excludes):
            continue
        if not GENERATED_TREES.isdisjoint(Path(key).parts):
            continue
        target = root / key
        if target.is_file() and is_text(target):
            out.append(key)
    return out


def home_path_findings(lineno: int, line: str) -> list[tuple[int, str, str]]:
    """Absolute home paths on this line that name a real account.

    The exposure this gates was a maintainer's own path reaching a public comment, so the unit
    is the raw line rather than stripped prose. A path is the same exposure in a JSON config
    value, in a fenced transcript pasted from a terminal, and in a sentence.
    """
    out = []
    for m in HOME_PATH.finditer(line):
        if m.group('user').lower() in SERVICE_ACCOUNTS:
            continue
        out.append((lineno, 'home-path',
                    f'absolute home path {m.group(0)!r} -> use a constructed path, not an '
                    'observed one'))
    return out


def operational_checkout(root: Path) -> bool:
    """Whether this checkout is an operational repository, read from what it carries.

    `spec/files.json` declares `repo-config/operational/develop.json` for the operational model
    and `repo-config/develop.json` for the release one, so a repository states its own model and
    nothing has to reach the hub registry to ask. The hub itself carries both payloads, being the
    template for each, so carrying the release payload decides it.
    """
    return ((root / 'repo-config' / 'operational' / 'develop.json').is_file()
            and not (root / 'repo-config' / 'develop.json').is_file())


def repo_prefix(root: Path) -> str:
    """Where `root` sits inside its repository, as a posix prefix, or '' when git cannot say.

    The generated-tree decision has to be made against the repository-relative path. Reading the
    filesystem path instead lets a directory *above* the checkout decide it, so a repository
    cloned under a parent named `reports` had its own `reports/` tree scanned as authored.
    """
    try:
        r = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-prefix'],
                           capture_output=True, text=True)
    except (OSError, ValueError):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''


def repo_root(path: Path) -> str:
    """The repository top level containing `path`, or '' when git cannot say."""
    start = path if path.is_dir() else path.parent
    try:
        r = subprocess.run(['git', '-C', str(start), 'rev-parse', '--show-toplevel'],
                           capture_output=True, text=True)
    except (OSError, ValueError):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''


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
        # Judge against the repository-relative path, never the filesystem one.
        # A directory above the checkout must not decide whether a file is generated.
        # An absolute argument otherwise carried its whole parent chain into the test.
        prefix = repo_prefix(root)
        for q in tracked:
            try:
                inside = Path(prefix) / q.relative_to(root)
            except ValueError:
                # Unreachable while both come from the same root, and kept safe rather than tidy.
                # With no repository-relative path there is nothing to judge, so scan the file.
                # Skipping on doubt is how a gate reports clean over what it never read.
                found.append(q)
                continue
            if GENERATED_TREES.isdisjoint(inside.parts):
                found.append(q)
            elif not GENERATED_TREES.isdisjoint(Path(prefix).parts):
                # The root named is itself inside a generated tree, so it was asked for.
                found.append(q)
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
# An ordered marker introduces the same construct, so `1. **Label** - ...` is one too.
LABEL_DASH = re.compile(r'^\s*(?:[-*]|[0-9]+\.)\s+\*\*[^*]+\*\*[.:]?\s+-\s+')

# The negative lookbehind keeps a word-joining character from starting a repetition:
# "either/or or must-pair" is one phrase followed by a conjunction, not a doubled word.
DUPWORD = re.compile(r'(?<![\w/-])(\w+)\s+\1\b', re.IGNORECASE)
SENT_END = re.compile(r'[.!?:]["\')\]]?\s*$')

# US English is a repo-wide rule, and the cspell gate reads README and HISTORY only.
# A British spelling anywhere else in the tree therefore reaches main unchallenged.
# Each family generates its own inflections, since an inflected spelling is as wrong as its base.
# A hand-listed family drifts the moment one form is added without the others.
# The cross product also generates forms no stem takes, which simply never match.
# `analyses` is omitted, being the US plural of `analysis` as much as a British verb form.
# `cancelled` is omitted, being the GitHub Actions job status rather than prose.
ISE_STEMS = ('author', 'custom', 'initial', 'maxim', 'minim', 'normal', 'optim', 'organ',
             'priorit', 'recogn', 'serial', 'special', 'standard', 'summar', 'synchron',
             'util', 'visual')
ISE_ENDINGS = (('ise', 'ize'), ('ised', 'ized'), ('ises', 'izes'), ('ising', 'izing'),
               ('isation', 'ization'), ('isations', 'izations'))
OUR_STEMS = ('behavi', 'col', 'fav', 'flav', 'hon', 'lab', 'neighb')
OUR_ENDINGS = ('', 's', 'ed', 'ing', 'al', 'ally', 'ful', 'ite', 'ites')
RE_STEMS = ('cent', 'fib', 'lit', 'met', 'theat')
RE_ENDINGS = (('re', 'er'), ('res', 'ers'), ('red', 'ered'))
# The spellings that follow no family, each with the US form that replaces it.
BRITISH_ODD = {
    'analyse': 'analyze', 'analysed': 'analyzed', 'analysing': 'analyzing',
    'artefact': 'artifact', 'artefacts': 'artifacts',
    'catalogue': 'catalog', 'catalogues': 'catalogs', 'catalogued': 'cataloged',
    'defence': 'defense', 'defences': 'defenses',
    'fulfil': 'fulfill', 'fulfils': 'fulfills', 'fulfilment': 'fulfillment',
    'judgement': 'judgment', 'judgements': 'judgments',
    'labelled': 'labeled', 'labelling': 'labeling',
    'licence': 'license', 'licences': 'licenses',
    'modelled': 'modeled', 'modelling': 'modeling',
    'offence': 'offense', 'offences': 'offenses',
    'practise': 'practice', 'practised': 'practiced', 'practising': 'practicing',
    'programme': 'program', 'programmes': 'programs',
    'signalled': 'signaled', 'signalling': 'signaling',
    'travelled': 'traveled', 'travelling': 'traveling',
    'whilst': 'while',
}


def british_spellings() -> dict[str, str]:
    """Every banned spelling mapped to the US form that replaces it."""
    words = dict(BRITISH_ODD)
    for stem in ISE_STEMS:
        words.update({stem + gb: stem + us for gb, us in ISE_ENDINGS})
    for stem in OUR_STEMS:
        words.update({f'{stem}our{end}': f'{stem}or{end}' for end in OUR_ENDINGS})
    for stem in RE_STEMS:
        words.update({stem + gb: stem + us for gb, us in RE_ENDINGS})
    return words


BRITISH = british_spellings()
# Longest alternative first, so an inflection is read whole rather than as its base word.
BRITISH_RE = re.compile(r'\b(?:' + '|'.join(sorted(BRITISH, key=len, reverse=True)) + r')\b',
                        re.IGNORECASE)


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
# The dash leads `quote_after` so the set does not read as a character range.
YAML: Syntax = {**HASH, 'raw': "'", 'quote_after': '-:,[{', 'escape_in': '"',
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

# A comment that is only a URI is a reference, not a sentence, so neither case nor wrap applies.
# It cannot be capitalized or restructured without corrupting the address it exists to carry.
# A URI inside a sentence is still prose, so the whole body has to be the address and nothing else.
# The angle brackets are matched as a pair or not at all.
# One bracket alone is a typo, and exempting it would hide the typo rather than report it.
# The scheme is case-insensitive per RFC 3986, so an uppercase one is the same reference.
BARE_URI = re.compile(r'^(?:<(?:https?|ftp)://[^>\s]+>|(?:https?|ftp)://[^>\s]+)$', re.IGNORECASE)

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
RUN_ON = re.compile(r'(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)(?:(?<!\.)\.|[!?])\s+(?=[A-Za-z])')

# A step marker opening a comment is a label on the sentence that follows, not a sentence of its own.
# `# 1. Deploy the hook.` is one sentence, and reading the marker's dot as a terminator made it two.
# It is stripped before the sentence checks so both the run-on and the opening-case test see the prose.
ENUM_PREFIX = re.compile(r'^\d+[.)]\s+')

# A comment body that is one token closing on a colon is a key or a heading, not a sentence.
# `# ignore:` above a commented-out block is disabled configuration.
# Capitalizing it corrupts the key a reader uncomments, so the rule would damage the file.
# The token count carries the test, since a colon ending real prose always has words before it.
KEY_ONLY = re.compile(r'^\S+:$')

# A label opening a definition names the thing being defined, so it is not the sentence's first word.
# `#   publish - 'true' when ...` documents an output named `publish`.
# Capitalizing it renames the output the workflow declares.
# This is the comment spelling of the `- **Label** - text` construct LABEL_DASH exempts in Markdown.
# It is tested where a line opens a definition, never where one continues a wrapped sentence.
# A continuation whose first word is followed by a spaced dash is a parenthetical instead.
# That is the construction the dash rule exists to catch, so exempting it would hide the violation.
# Both live instances in the tree are continuations, which is what scoped this to the case branch.
COMMENT_LABEL = re.compile(r'^[A-Za-z_][\w.-]*\s+-\s+')
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


# A bullet's `**Label**:` opens the text the same way `- **Label** -` does.
# Its colon introduces the bullet rather than a list, and reading it as one excused the splice.
# The colon is written inside the emphasis as often as outside it, and both spell one construct.
# Matching `**Label**:` alone left `**Label:**` announcing a list it never announced.
LABEL_COLON = re.compile(r'^\s*(?:[-*]|[0-9]+\.)\s+\*\*[^*]+?(?:\*\*\s*:|:\s*\*\*)')

# A sentence boundary inside one line, so a list exemption is scoped to the sentence holding it.
# The guards are the run-on rule's, so an initial or an abbreviation ends nothing.
# The trailing class is the emphasis or bracket a Markdown sentence closes inside.
# Reading a bare `. ` instead left `.**` and `.)` joining a bullet's every sentence into one span.
SENTENCE_BREAK = re.compile(r'(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)'
                            r'[.!?][*_`"\')\]]*\s+')


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
    cells = s.strip().strip('|').split('|') if s.lstrip().startswith('|') else [s]
    return [LABEL_COLON.sub('', cell) for cell in cells]


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
        if (not body or NOT_PROSE.search(body) or BARE_URI.match(body.strip())
                or KEY_ONLY.match(body)):
            prev_body = ''
            continue
        # An unpunctuated Markdown HTML comment is a structural marker, not commentary.
        # It is a label, so it takes neither a capital nor a sentence split.
        # A tool matches each one verbatim, so rewriting it breaks whatever reads it.
        # Group headers, the ToC-omit directive, and the agent-safety markers are the cases.
        # A comment that does punctuate a sentence is prose and is judged as prose.
        if path.suffix == '.md' and not SENT_END.search(body):
            prev_body = ''
            continue
        body = ENUM_PREFIX.sub('', body)
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
        # A label opening a definition is exempt, since the lowercase word is the name being defined.
        elif leading and body[:1].islower() and not COMMENT_LABEL.match(body):
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
    # Outside Markdown the prose lives in the comments, and both rules judge prose, not code.
    # A source line holds identifiers and literals, and an attribute value may legally repeat.
    # Reading it rejects correct work, `class="gallery gallery-cols-1"` being the reported case.
    comments: dict[int, list[str]] = {}
    if {'spelling', 'dupword'} & rules and path.suffix != '.md':
        for ln, text, _ in extracted_comments(path, lines):
            comments.setdefault(ln, []).append(text)
    in_fence = False
    prev_txt = ''
    prev_no = 0
    for i, line in enumerate(lines, 1):
        line = line.rstrip('\r')
        # Judged before the fence and inline-code handling below, deliberately.
        # A path pasted inside a fenced transcript is the same exposure as one in a sentence.
        if 'home-path' in rules:
            out.extend(home_path_findings(i, line))
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

        # Both prose rules are Markdown-only until a comment can be told from code.
        # A shell script carries 78 statement separators that are not prose at all.
        if path.suffix == '.md':
            if 'semicolon' in rules:
                for span in list_spans(prose):
                    # The sentence is the unit, since the list an exemption protects lives in one.
                    # Judged over a whole bullet, one colon exempted every semicolon after it.
                    for sentence in sentences(span):
                        # A list keeps its semicolons, announced by a colon or a second separator.
                        # The comma qualifies the list rather than one separator's position.
                        # An enumeration whose commas fall in a later item keeps every semicolon.
                        # Read positionally, it split one series and flagged that series' openers.
                        listish = sentence.count(';') > 1 or ':' in sentence.split(';')[0]
                        if listish and ',' in sentence:
                            continue
                        for _ in SEMICOLON.finditer(sentence):
                            out.append((i, 'semicolon',
                                        'semicolon in prose -> a comma or two sentences'))
            if 'dash' in rules:
                skip = LABEL_DASH.match(prose)
                for m in DASH.finditer(prose):
                    if skip and m.start() < skip.end():
                        continue
                    out.append((i, 'dash',
                                'spaced hyphen -> a comma, two sentences, or parentheses'))

        if 'dupword' in rules:
            # Each comment on the line is judged on its own rather than joined with its neighbors.
            # Joining them would read the second's opening word as a repeat of the first's last.
            texts = ([prose] if path.suffix == '.md'
                     else [strip_inline_code(c) for c in comments.get(i, [])])
            for text in texts:
                for m in DUPWORD.finditer(text):
                    if m.group(0).lower() in DUP_ALLOW:
                        continue
                    out.append((i, 'dupword', f"duplicated word '{m.group(1)}'"))

        if 'spelling' in rules:
            texts = [prose] if path.suffix == '.md' else comments.get(i, [])
            for m in BRITISH_RE.finditer(strip_inline_code(' '.join(texts))):
                found = m.group(0)
                out.append((i, 'spelling',
                            f"British spelling '{found}' -> '{us_form(found)}'"))

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

    # An operational repository's runbook carries the literal path an operator types.
    # That is the repository's own content, not an agent quoting an environment it observed.
    # The skip is announced, since a rule that silently stops running reads as one that passed.
    # That is the same failure the diff-scope floor below exists to prevent.
    if 'home-path' in rules and operational_checkout(Path(repo_root(Path('.')) or '.')):
        rules.discard('home-path')
        print('note: home-path is not checked in an operational repository, where an absolute '
              'path is the operator instruction rather than observed data.', file=sys.stderr)

    # Checked before discovery, which reads every tracked file to classify it as text.
    # A run this rejects would otherwise pay that cost and throw the result away.
    # `--list-files` is exempt, since it reports the scan scope and never consults the diff.
    if a.diff and not a.list_files:
        # `git diff` runs in the current directory while the paths may name another checkout.
        # Scanning one repository and diffing another intersects to nothing.
        # The run then reports clean, which is the false clean this gate exists to prevent.
        # It cost a real verification once, where a branch read zero from the wrong directory.
        # A path under no repository at all fails the same way, and more quietly.
        # Discovery walks the filesystem, then every absolute key misses the repo-relative ones.
        # Requiring the same root covers both, where testing for a different one did not.
        here = repo_root(Path('.'))
        for raw in (a.paths or ['.']):
            there = repo_root(Path(raw))
            if here and there != here:
                where = there or 'no git repository'
                print(f'error: --diff resolves against {here}, but {raw} is in {where}. '
                      'Run the gate from the repository being scanned, since a diff taken '
                      'elsewhere scopes every finding away and reports a false clean.',
                      file=sys.stderr)
                return 2

    files = discover(a.paths or ['.'], tuple(a.exclude))

    if a.list_files:
        for f in files:
            print(rel(f))
        return 0

    scope = changed_lines(a.diff) if a.diff else None
    if a.diff and scope is None:
        # Widening to the whole tree answers a different question, and answers it silently.
        # A caller scoping to a change gets the backlog reported as though the change made it.
        # A CI adoption hits this first, where an unresolvable base walls off the first run.
        # Scoping to nothing instead would report a false clean, so neither default is honest.
        print(f'error: cannot diff against {a.diff!r}, so the run cannot be scoped to changed '
              'lines. Refusing to scan the whole tree instead, since that reports the existing '
              'backlog as though this change introduced it. Check the ref exists and that the '
              'checkout carries its history.', file=sys.stderr)
        return 2
    if scope is not None:
        matched = [f for f in files if rel(f) in scope]
        # The floor every verdict below rests on, asserted rather than guarded.
        # Each route to a false clean so far was closed after a reviewer saw it.
        # The next is closed that way or not at all, which is what a floor covers.
        # A run that resolves a non-empty diff and matches none of its files failed to scope.
        # Zero alone is not the test.
        # A change touching only files the rules do not read matches nothing and is clean.
        # An image or a lock file is that case.
        # So the comparison is against the diff's own list of files this run could have read.
        if scope and not matched:
            unread = unread_diff_files(scope, a.paths or ['.'], tuple(a.exclude))
            if unread:
                shown = ', '.join(unread[:5]) + (' and more' if len(unread) > 5 else '')
                print(f'error: the diff against {a.diff!r} names {len(unread)} readable file(s) '
                      f'this run was asked about, and the scan matched none of them: {shown}. '
                      'Refusing to report a clean run, since a gate that read nothing is '
                      'indistinguishable from a gate with nothing to read. Check that the run '
                      'starts at the repository top level and that the requested paths cover '
                      'the change.', file=sys.stderr)
                return 2
        files = matched

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
