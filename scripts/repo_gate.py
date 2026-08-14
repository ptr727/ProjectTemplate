#!/usr/bin/env python3
"""Deterministic pre-push checks for GOVERNANCE.md rules nothing else enforces.

Each check maps to a recurring review-finding category (counts from a 1,047-finding audit
of this repo's Copilot reviews):

  sha-pin       Action SHA-pinning gaps                        25 findings  (GOVERNANCE.md rule)
  eol           .editorconfig <-> .gitattributes disagreement  40 findings
  eol-coverage  A line-ending pin the tree disagrees with      ptr727/ProjectTemplate#633

`eol-coverage` is the direction `eol` does not have. Both line-ending documents can agree
perfectly and both be wrong about the repository they describe, which is how `ptr727/Blog`
carried an unpinned shebang script systemd runs unattended, and two pins naming paths never
tracked there, while this gate reported clean over the lot. The dead pin is the worse half and
the reason the other survived: the comment above those two asserted the extensionless case was
handled, so the one real instance twenty lines up read as covered by every human and agent who
opened the file.

`sha-pin` reads the shape and then resolves it, because forty hex characters is a format any
fabricated string satisfies, and an agent hand-writing a plausible SHA into a workflow is a
failure this repo has seen rather than a hypothetical one. Resolving also catches the
neighboring case, a pin whose commit was reachable only from a branch since squashed and
deleted, which breaks a downstream gate long after the change that caused it. The
`gh-write-guard` hook cannot cover either, since it watches Bash and an editor tool writing
the same string into a file never reaches it.

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

import argparse
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

# GOVERNANCE.md documents exactly one floating-ref exception.
SHA_EXCEPTIONS = {'dotnet/nbgv'}
USES = re.compile(r'^\s*-?\s*uses:\s*(?P<ref>[^\s#]+)', re.MULTILINE)
PIN = re.compile(r'^[0-9a-f]{40}$')
WORKFLOW = re.compile(r'workflows/.*\.ya?ml$')
# What `gh` prints when GitHub answered, as opposed to when nothing was reached at all.
HTTP_STATUS = re.compile(r'\(HTTP (\d{3})\)')
# The two GitHub returns for an object that is not there.
# Everything else is a failure to read, since 401 and 403 are credentials and a rate limit.
# A network error carries no status at all.
# Reading any of those as absence fails a correct pin, which is the direction that costs most.
ABSENT = {'404', '422'}
GH_TIMEOUT = 20

# The token a .gitattributes comment carries to exempt the pins below it from the dead reading.
# A carried baseline pin goes live the moment a consumer adds the file it names.
# The mark travels with that copy, so the consumer holding the baseline stays exempt too.
# It reaches to the next blank line, the way the file already groups a pin with its rationale.
FORWARD = 'forward-declared'
SHEBANG = b'#!'

# How a check says it did less than its name.
# A gate that quietly degrades to a weaker reading prints the same clean line as one that ran.
# The narrowing is therefore printed rather than left to be inferred.
# Never a finding, since nothing is wrong with the tree when the network is what is missing.
NOTES: list[str] = []


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout


def origin_owner(root: Path) -> str | None:
    """The owner of the repository at `root`, lower-cased, or None where it cannot be read.

    Read from the tree being scanned rather than from this script's own checkout, since the gate
    is hub-hosted and runs against whatever `--root` names.
    """
    url = sh('git', '-C', str(root), 'remote', 'get-url', 'origin').strip()
    m = re.search(r'[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$', url)
    return m.group(1).lower() if m else None


def gh_exists(path: str) -> bool | None:
    """True where GitHub returned the object, False where it answered absent, None where neither.

    None covers an absent `gh`, no credentials, a rate limit, and an offline host, which are one
    thing here: nothing was learned. The caller reports those as skipped rather than as findings,
    so the gate stays usable on a machine with no network instead of failing a correct tree.
    """
    try:
        r = subprocess.run(['gh', 'api', path, '--jq', '.sha'],
                           capture_output=True, text=True, timeout=GH_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return True
    m = HTTP_STATUS.search(r.stderr)
    return False if m and m.group(1) in ABSENT else None


def pin_resolves(nwo: str, sha: str, cache: dict[tuple[str, str], bool | None]) -> bool | None:
    """Whether `sha` is a commit in `nwo`, cached, with an unreadable repository read as unknown.

    An absent commit and a repository the credentials cannot see are the same 404 from here, and
    reading the second as the first fails a correct pin whenever the token is narrower than the
    fleet, which a repository-scoped CI token is. So a miss is confirmed against the repository
    itself before it becomes a finding, and that second read runs only on the failing path.
    """
    key = (nwo, sha)
    if key not in cache:
        seen = gh_exists(f'repos/{nwo}/commits/{sha}')
        if seen is False and gh_exists(f'repos/{nwo}') is not True:
            seen = None
        cache[key] = seen
    return cache[key]


def tracked(root: Path) -> list[str]:
    out = sh('git', '-C', str(root), 'ls-files')
    return [l for l in out.split('\n') if l]


def workflow_files(files: list[str]) -> list[str]:
    """The files sha-pin governs, exposed so a test can assert the scan found something.

    A scan that matches nothing reports `0 issue(s)`, indistinguishable from a clean tree.
    """
    return [f for f in files if WORKFLOW.search(f)]


def shebang_files(root: Path, files: list[str]) -> list[str]:
    """The tracked files opening `#!`, exposed so a test can assert the scan found something.

    The shebang is the test rather than the executable bit, because the two move independently and
    it is the interpreter line a CRLF breaks. A path git lists but the working tree does not carry
    is skipped, since `git ls-files` names what the index holds.
    """
    out = []
    for rel in files:
        try:
            with (root / rel).open('rb') as fh:
                if fh.read(2) == SHEBANG:
                    out.append(rel)
        except OSError:
            continue
    return out


def eol_pins(text: str) -> list[tuple[str, bool]]:
    """Every glob `.gitattributes` pins to LF, each with whether its block is forward-declared.

    One parser for both line-ending checks, so the two cannot come to read different pin sets out
    of the same file. Exposed so a case can read the marking without building a tree around it.
    """
    out, marked = [], False
    for raw in text.split('\n'):
        line = raw.strip()
        if not line:
            marked = False                              # a blank line closes the block and its mark
            continue
        if line.startswith('#'):
            marked = marked or FORWARD in line
            continue
        m = re.match(r'(\S+)\s+.*eol=lf', line)
        if m:
            out.append((m.group(1), marked))
    return out


def attr_glob(pattern: str) -> re.Pattern[str]:
    """One `.gitattributes` pattern as a regex over a repo-relative path.

    Deliberately not `git ls-files -- <pattern>`, which is pathspec matching and a different
    language: there `*` crosses a `/`, so `capture/*.py` also matches `capture/sub/x.py` and a dead
    pin reads as live. A pattern carrying no `/` matches the basename at any depth, one carrying a
    `/` anchors at the root, `*` and `?` stop at a separator, and a whole segment of `**` is the one
    form that crosses them, which is what `Docker/s6-overlay/**` needs.
    """
    # A leading slash is itself the anchor, so it is read before it is stripped.
    # `/uv.lock` binds at the root where the same pattern without it binds at any depth.
    p = pattern.rstrip('/')
    body, parts = '', p.lstrip('/').split('/')
    for i, seg in enumerate(parts):
        last = i == len(parts) - 1
        if seg == '**':
            body += '.*' if last else '(?:[^/]+/)*'
        else:
            body += _segment(seg) + ('' if last else '/')
    # An unanchored pattern is matched against the basename, so any leading directories are free.
    return re.compile(('' if '/' in p else '(?:.*/)?') + body + r'\Z')


def _segment(seg: str) -> str:
    """One path segment of a glob as a regex, with `*` and `?` held inside the segment."""
    out, i = '', 0
    while i < len(seg):
        c = seg[i]
        if c == '*':
            out += '[^/]*'
        elif c == '?':
            out += '[^/]'
        elif c == '[':
            j = i + 1
            j += 1 if j < len(seg) and seg[j] in '!^' else 0
            j += 1 if j < len(seg) and seg[j] == ']' else 0
            while j < len(seg) and seg[j] != ']':
                j += 1
            if j >= len(seg):                           # an unclosed class is a literal bracket
                out += re.escape(c)
                i += 1
                continue
            out += '[' + ('^' + seg[i + 2:j] if seg[i + 1] in '!^' else seg[i + 1:j]) + ']'
            i = j + 1
            continue
        else:
            out += re.escape(c)
        i += 1
    return out


def resolved_eol(root: Path, paths: list[str]) -> dict[str, str] | None:
    """What git resolves `eol` to for each path, or None where git did not answer at all.

    Asked of git rather than re-derived from `.gitattributes`, so this cannot disagree with what a
    checkout actually applies. NUL-delimited in both directions, since a tracked path may carry a
    space or a colon and the newline form splits those wrongly.
    """
    if not paths:
        return {}
    try:
        r = subprocess.run(['git', '-C', str(root), 'check-attr', '-z', '--stdin', 'eol'],
                           input='\0'.join(paths) + '\0', capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    f = r.stdout.split('\0')
    return {f[i]: f[i + 2] for i in range(0, len(f) - 2, 3)}


def check_sha_pin(root: Path, files: list[str]) -> list[str]:
    """Every `uses:` naming an action is a 40-hex SHA, and one under this owner is a SHA that
    resolves. A ref into this repository's own tree names no action and is skipped.

    Resolution is scoped to the scanned repository's own owner, because that is where the fleet's
    own actions live and where the decay this catches comes from: a squash merge deletes the
    branch a pin was taken from, and the pin outlives the commit. A third-party action's tag is
    stable by comparison, and reading one would make every local run of this gate depend on a
    stranger's repository answering. The cost is stated rather than left to be found, and it is
    real: a fabricated pin on a third-party action is still only shape-checked here. The counts
    below print on every run so that narrowness is visible rather than inferred from a clean line.
    """
    bad = []
    owner = origin_owner(root)
    cache: dict[tuple[str, str], bool | None] = {}
    resolved = foreign = unowned = unread = 0
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
            line = text[:m.start()].count('\n') + 1
            if not PIN.match(ver):
                bad.append(f'{rel}:{line}: `{action}@{ver}` is a floating ref, not a 40-hex SHA')
                continue
            # An action reference is `owner/repo` with an optional path to the action within it.
            nwo = '/'.join(action.split('/')[:2])
            # Counted apart from a known other owner, since the two are not the same state.
            # The note exists to describe the narrowing exactly, so it must not merge them.
            # A checkout with no readable origin skips every pin, this owner's own included.
            if owner is None:
                unowned += 1
                continue
            if nwo.split('/')[0].lower() != owner:
                foreign += 1
                continue
            state = pin_resolves(nwo, ver, cache)
            if state is False:
                bad.append(f'{rel}:{line}: `{action}@{ver}` is shaped like a SHA and resolves to '
                           f'no commit in {nwo}')
            elif state is None:
                unread += 1
            else:
                resolved += 1
    # Unconditional, so an all-zero run is as visible as a count rather than a clean line.
    # A guard on a non-zero counter hides the run that resolved nothing, which is this one.
    NOTES.append(f'resolved {resolved} pin(s) against GitHub. Read for shape only: '
                 f'{foreign} under another owner, {unowned} whose owner could not be '
                 f"compared because this checkout's origin is unreadable, "
                 f'{unread} GitHub did not answer for.')
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
    ga_lf = {g for g, _ in eol_pins(ga.read_text(encoding='utf-8', errors='replace'))}
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
    # A global LF default satisfies the lookup below for any path, an absent one included.
    # On a repo shaped that way this is vacuously true for every pin it will ever read.
    # Said out loud, since the result otherwise renders as an ordinary clean line.
    if any(p in ('*', '**') for p in ec_pats):
        NOTES.append('.editorconfig sets end_of_line = lf globally, so that default satisfies '
                     'every pin and nothing here read pin content. Use eol-coverage instead.')
    out = []
    for g in sorted(ga_lf):
        for cand in expand(g):
            if any(fnmatch(cand, p) or fnmatch(cand, p.lstrip('/')) or
                   fnmatch(PurePosixPath(cand).name, p) for p in ec_pats):
                break
        else:
            out.append(f'.gitattributes pins `{g}` to LF with no matching .editorconfig override')
    return out


def check_eol_coverage(root: Path, files: list[str]) -> list[str]:
    """The line-ending pins read against the tree, which is what `eol` never does.

    `eol` compares .gitattributes with .editorconfig, so both can agree and both be wrong about the
    repository they describe. Two shapes live in that gap. A tracked shebang script git does not
    hold at LF is the live break, since a CRLF interpreter line fails at exec. A pin matching no
    tracked file is the quieter one, and it is what hides the first: a dead pin reads as coverage,
    so the class it names is taken as handled and nobody looks for the instance that is not.

    A pin block marked `forward-declared` is exempt from the second reading only. The first has no
    such case, since a shebang script that exists is a script that runs.
    """
    ga = root / '.gitattributes'
    if not ga.exists():
        return ['missing .gitattributes']
    pins = eol_pins(ga.read_text(encoding='utf-8', errors='replace'))
    shebangs = shebang_files(root, files)
    out = []

    attrs = resolved_eol(root, shebangs)
    if attrs is None:
        # Nothing was read rather than nothing was wrong, which are the same clean line otherwise.
        NOTES.append('git did not answer `check-attr`, so no shebang file was read at all.')
    else:
        for rel in shebangs:
            got = attrs.get(rel, 'unspecified')
            if got != 'lf':
                out.append(f'{rel}: a shebang script git resolves to `eol: {got}`, not `lf`')

    for glob, forward in pins:
        if forward or any(attr_glob(glob).match(f) for f in files):
            continue
        out.append(f'.gitattributes pins `{glob}` to LF and no tracked file matches it')

    marked = sum(1 for _, forward in pins if forward)
    NOTES.append(f'read {len(pins)} LF pin(s), {marked} of them forward-declared, over '
                 f'{len(shebangs)} shebang file(s) in {len(files)} tracked file(s).')
    return out


CHECKS = {'sha-pin': check_sha_pin, 'eol': check_eol, 'eol-coverage': check_eol_coverage}


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
        # Cleared per check, so a note is attributed to the check that raised it.
        NOTES.clear()
        hits = CHECKS[name](root, files)
        status = 'FAIL' if hits else 'ok'
        print(f'[{status:4}] {name:12} {len(hits)} issue(s)')
        for h in hits:
            print(f'         {h}')
        # After the findings and outside the count, since a note is not one.
        for note in NOTES:
            print(f'         note: {note}')
        total += len(hits)
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())
