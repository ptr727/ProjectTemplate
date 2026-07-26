#!/usr/bin/env python3
"""Enforce the GOVERNANCE.md "Documentation Style Conventions" rules no other linter checks.

markdownlint, cspell, actionlint, and editorconfig-checker all pass on prose that breaks
these rules, so nothing enforced them before this script. Rules implemented:
  ascii          Write ASCII in all agent-authored text.
  semicolon      No semicolon joining two independent clauses.
  dupword        No duplicated consecutive word.
  sentence-split A sentence must not wrap across lines (one sentence per line).

Exit 1 if any violation is found. Read-only; never edits.
"""
from __future__ import annotations
import argparse, re, subprocess, sys, unicodedata
from pathlib import Path


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

# Typographic Unicode the rule says to replace with its ASCII equivalent on sight.
# AGENTS.md allows two narrow exceptions that are deliberately NOT flagged:
# scientific/technical symbols with no clean ASCII equivalent (ohm, micro, degree,
# pi, superscripts, section sign) and developer-typed Unicode such as emoji.
# Only substitutable typography appears here.
SUGGEST = {
    '\u2014': '-', '\u2013': '-', '\u2018': "'", '\u2019': "'",
    '\u201c': '"', '\u201d': '"', '\u2026': '...', '\u00a0': ' ',
    '\u2022': '-', '\u2011': '-', '\u2192': '->',
}

# A semicolon splice: "<clause>; <pronoun/article/subject> <verb>..."
# Deliberately conservative - only flags a lowercase word after "; " that starts
# a clause with a following finite verb. List semicolons and code are not matched.
SPLICE = re.compile(
    r';\s+(?P<w>it|this|that|they|he|she|we|you|the|a|an|there|these|those)\s+\w+',
    re.IGNORECASE)

DUPWORD = re.compile(r'\b(\w+)\s+\1\b', re.IGNORECASE)
SENT_END = re.compile(r'[.!?]["\')\]]?\s*$')
CODE_FENCE = re.compile(r'^\s*(```|~~~)')

DUP_ALLOW = {'that that', 'had had', 'the the'}  # 'the the' still flagged below


def strip_inline_code(s: str) -> str:
    return re.sub(r'`[^`]*`', '``', s)


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

        if 'semicolon' in rules:
            m = SPLICE.search(txt)
            if m:
                out.append((i, 'semicolon', f"semicolon splice before '{m.group('w')}'"))

        if 'dupword' in rules:
            for m in DUPWORD.finditer(txt):
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='*', default=['.'])
    ap.add_argument('--check', action='append', dest='checks',
                    choices=['ascii', 'semicolon', 'dupword', 'sentence-split'])
    ap.add_argument('--ext', action='append', default=None)
    ap.add_argument('--exclude', action='append', default=[])
    ap.add_argument('--summary', action='store_true')
    ap.add_argument('--diff', metavar='BASE',
                    help='only report violations on lines changed vs BASE '
                         '(matches the repo policy: fix as each file is next edited, not swept)')
    a = ap.parse_args()

    rules = set(a.checks or ['ascii', 'semicolon', 'dupword'])
    exts = set(a.ext or ['.md', '.py', '.yml', '.yaml', '.sh', '.cs', '.json'])
    excl = ['.git/', 'node_modules/', '__pycache__/', '.venv/'] + a.exclude

    files: list[Path] = []
    for p in a.paths:
        pp = Path(p)
        files.extend([f for f in ([pp] if pp.is_file() else pp.rglob('*'))
                      if f.is_file() and f.suffix in exts
                      and not any(x in str(f).replace('\\', '/') for x in excl)])

    scope = changed_lines(a.diff) if a.diff else None
    if a.diff and scope is None:
        print('warning: git diff failed; falling back to whole-tree scan', file=sys.stderr)
    if scope is not None:
        files = [f for f in files
                 if str(f).replace('\\', '/').lstrip('./') in scope]

    total = 0
    bykind: dict[str, int] = {}
    byfile: dict[str, int] = {}
    for f in sorted(set(files)):
        allowed = scope.get(str(f).replace('\\', '/').lstrip('./')) if scope is not None else None
        for ln, kind, msg in check_file(f, rules):
            if allowed is not None and ln not in allowed:
                continue
            total += 1
            bykind[kind] = bykind.get(kind, 0) + 1
            byfile[str(f)] = byfile.get(str(f), 0) + 1
            if not a.summary:
                print(f'{f}:{ln}: {kind}: {msg}')

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
