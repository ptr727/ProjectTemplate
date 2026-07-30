#!/usr/bin/env python3
"""Deterministic pre-push checks for GOVERNANCE.md rules nothing else enforces.

Each check maps to a recurring review-finding category (counts from a 1,047-finding audit
of this repo's Copilot reviews):

  sha-pin     Action SHA-pinning gaps                      25 findings  (GOVERNANCE.md rule)
  eol         .editorconfig <-> .gitattributes disagreement 40 findings

A stale-backticked-path check was built and REJECTED: in a template repo, docs
legitimately reference paths that live in downstream repos (`.vscode/tasks.json`,
`Docker/README.md`, `reports/*/audit.md` targets), so it produced 34 false positives
on a clean tree with no way to separate those from real drift. Doc-to-doc drift is
the job of the fresh-context self-review, not a regex.

Read-only. Exit 1 if any check fails. Pair with prose_lint.py, which covers the
house-style prose rules, and with the existing CI linters (markdownlint, cspell,
actionlint, editorconfig-checker, spec/validate.py).
"""
from __future__ import annotations
import argparse, re, subprocess, sys
from pathlib import Path, PurePosixPath
from fnmatch import fnmatch

# GOVERNANCE.md documents exactly one floating-ref exception.
SHA_EXCEPTIONS = {'dotnet/nbgv'}
USES = re.compile(r'^\s*-?\s*uses:\s*(?P<ref>[^\s#]+)', re.M)
PIN = re.compile(r'^[0-9a-f]{40}$')
WORKFLOW = re.compile(r'workflows/.*\.ya?ml$')


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout


def tracked(root: Path) -> list[str]:
    out = sh('git', '-C', str(root), 'ls-files')
    return [l for l in out.split('\n') if l]


def workflow_files(files: list[str]) -> list[str]:
    """The files sha-pin governs, exposed so a test can assert the scan found something.

    A scan that matches nothing reports `0 issue(s)`, indistinguishable from a clean tree.
    """
    return [f for f in files if WORKFLOW.search(f)]


def check_sha_pin(root: Path, files: list[str]) -> list[str]:
    bad = []
    for rel in workflow_files(files):
        try:
            text = (root / rel).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for m in USES.finditer(text):
            ref = m.group('ref')
            if ref.startswith(('./', '.github/')):      # local reusable workflow
                continue
            if '@' not in ref:
                bad.append(f'{rel}: `uses: {ref}` has no ref at all')
                continue
            action, _, ver = ref.rpartition('@')
            if action in SHA_EXCEPTIONS:
                continue
            if not PIN.match(ver):
                line = text[:m.start()].count('\n') + 1
                bad.append(f'{rel}:{line}: `{action}@{ver}` is a floating ref, not a 40-hex SHA')
    return bad


def check_eol(root: Path, files: list[str]) -> list[str]:
    """Every path pinned LF in .gitattributes has the matching .editorconfig override.

    GOVERNANCE.md: "Pair each such pin with a matching .editorconfig override - the git pin
    alone is not enough." Extensionless executables are the case this protects.

    One direction only. An .editorconfig LF glob with no .gitattributes pin is legitimate, since
    .editorconfig governs what the editor writes where git enforces a class it must not guess at.
    `files` is unused, and present so every check in CHECKS shares one signature.
    """
    ec, ga = root / '.editorconfig', root / '.gitattributes'
    missing = [name for name, p in (('.editorconfig', ec), ('.gitattributes', ga))
               if not p.exists()]
    if missing:
        return [f'missing {name}' for name in missing]
    # .gitattributes: "<glob> text eol=lf"
    ga_lf = set()
    for raw in ga.read_text(encoding='utf-8', errors='replace').split('\n'):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'(\S+)\s+.*eol=lf', line)
        if m:
            ga_lf.add(m.group(1))
    # .editorconfig: [glob] ... end_of_line = lf
    ec_lf: set[str] = set()
    cur = None
    for raw in ec.read_text(encoding='utf-8', errors='replace').split('\n'):
        line = raw.strip()
        if line.startswith('[') and line.endswith(']'):
            cur = line[1:-1]
        elif cur and re.match(r'end_of_line\s*=\s*lf\b', line):
            ec_lf.add(cur)

    def expand(g: str) -> list[str]:
        """Expand one level of {a,b,c} brace syntax, which EditorConfig supports."""
        m = re.search(r'\{([^{}]*)\}', g)
        if not m:
            return [g]
        return [x for part in m.group(1).split(',')
                for x in expand(g[:m.start()] + part + g[m.end():])]

    ec_pats = [p for e in ec_lf for p in expand(e)]
    out = []
    for g in sorted(ga_lf):
        for cand in expand(g):
            if any(fnmatch(cand, p) or fnmatch(cand, p.lstrip('/')) or
                   fnmatch(PurePosixPath(cand).name, p) for p in ec_pats):
                break
        else:
            out.append(f'.gitattributes pins `{g}` to LF with no matching .editorconfig override')
    return out


CHECKS = {'sha-pin': check_sha_pin, 'eol': check_eol}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--check', action='append', choices=sorted(CHECKS))
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    files = tracked(root)
    if not files:
        print(f'{root}: not a git repo or no tracked files', file=sys.stderr)
        return 2

    total = 0
    for name in (a.check or sorted(CHECKS)):
        hits = CHECKS[name](root, files)
        status = 'FAIL' if hits else 'ok'
        print(f'[{status:4}] {name:12} {len(hits)} issue(s)')
        for h in hits:
            print(f'         {h}')
        total += len(hits)
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())
