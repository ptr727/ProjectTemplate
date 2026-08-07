#!/usr/bin/env python3
"""Deterministic pre-push checks for GOVERNANCE.md rules nothing else enforces.

Each check maps to a recurring review-finding category (counts from a 1,047-finding audit
of this repo's Copilot reviews):

  sha-pin     Action SHA-pinning gaps                      25 findings  (GOVERNANCE.md rule)
  eol         .editorconfig <-> .gitattributes disagreement 40 findings

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
import argparse, re, subprocess, sys
from pathlib import Path, PurePosixPath
from fnmatch import fnmatch

# GOVERNANCE.md documents exactly one floating-ref exception.
SHA_EXCEPTIONS = {'dotnet/nbgv'}
USES = re.compile(r'^\s*-?\s*uses:\s*(?P<ref>[^\s#]+)', re.M)
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

# How a check says it did less than its name.
# A gate that quietly degrades to a weaker reading prints the same clean line as one that ran.
# The narrowing is therefore printed rather than left to be inferred.
# Never a finding, since nothing is wrong with the tree when the network is what is missing.
NOTES: list[str] = []


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout


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
                           capture_output=True, text=True, timeout=GH_TIMEOUT)
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


def check_sha_pin(root: Path, files: list[str]) -> list[str]:
    """Every workflow `uses:` is a 40-hex SHA, and one under this owner is a SHA that resolves.

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
    resolved = foreign = unread = 0
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
            if owner is None or nwo.split('/')[0].lower() != owner:
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
    if resolved or foreign or unread:
        NOTES.append(f'resolved {resolved} pin(s) against GitHub; {foreign} under another owner '
                     f'and {unread} GitHub did not answer for were read for shape only')
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
