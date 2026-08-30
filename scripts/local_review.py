#!/usr/bin/env python3
"""Record and verify that a local review pass covered this branch's current content.

`AGENTS.md` and the `local-strict-review` skill hold the rule about when a local review is owed.
This script is what makes that rule checkable rather than a second copy of it: a review pass
records a receipt keyed on the content it reviewed, and a capture point (a git pre-push hook, a
skill step, a session about to claim done) asks whether a receipt still covers what is about to
be pushed.

The key is over the content of the changed paths, plus the merge-base commit that fixes what
"changed" means. It is deliberately not over the diff text and not over HEAD, which is what lets a
review run before the commit while the check runs at the push: reviewing untracked work and then
committing it unchanged leaves the key identical, while changing one byte moves it.

Every identity in the key is computed by git rather than reconstructed here, which is the point
worth stating plainly, because reconstructing one is where this went wrong twice. A blob id read
off the working tree by hashing its raw bytes does not equal the id git stores whenever a filter
sits between them, and this repository's own `.gitattributes` applies one to every text file, so
`git add` moved the key on any CRLF file. A file mode derived from `stat` disagrees with git's on
any checkout where `core.fileMode` is off. So the working tree's own state is read by staging it
into a throwaway index that git builds, and the result is compared against the real index that git
already maintains. Both sides then carry git's own answer, filters, modes, renames, deletions,
symlinks, and submodules included.

Each path contributes the set of distinct states it is in, the throwaway index's and the real
one's. When they agree the set collapses to one member, which is what makes `git add` and
`git commit` invisible to the key. When they diverge, because content is staged that the working
tree no longer holds, or because a mode changed, the set has more than one member and the key
moves. Reading the real index rather than the working tree alone is what stops content being
committed that the key never saw.

What the key covers is the net content this branch introduces against its target, not the series
of commits that produced it, and the difference is worth stating plainly because two consequences
follow. An interactive rebase or an amend that leaves the tree byte-identical, changing only commit
messages or authorship, keeps the same key. So does a branch that adds a file in one commit and
deletes it in a later one: the commits carry content, the net result carries none, and the key
follows the net result.

That is the intended scope rather than a gap. A feature branch reaches `develop` as a squash and
`develop` reaches `main` as a merge, so the net diff is what actually lands, and it is what a
reviewer reads. Commit messages are governed by `git-commit-conventions` rather than by a content
review. But a capture point built on this must not be described as covering the commit series, and
a reviewer who needs to see intermediate churn has to read the range itself.

Two known cases move the key though nothing that would be pushed changed, both erring toward
demanding another review rather than skipping one. An intent-to-add entry (`git add -N`, which
some tooling issues implicitly) sits in the real index as the empty blob while the throwaway index
holds the real content. And a new untracked file that `.gitignore` does not cover enters the
changed set, since a file not yet added is exactly what a review has to read.

This script holds no review logic. It drives backends: `agent-skill` is the `local-strict-review`
subagent pass, which only a live agent session can run, so the engine records it rather than
invoking it. `coderabbit-cli` is headless and is the one backend a git hook can execute by itself.

A recorded pass means a review ran over exactly this content. It never means the content is clean.
Disposing of what a review found is judgment, per `pr-review-conduct`'s five outcomes, so a pass is
recorded whether the pass raised findings or none.

Usage: python3 scripts/local_review.py status [--target <branch>]
           what the current content digest is, and which backends hold a pass on it.
       python3 scripts/local_review.py record --reviewer <id> [--target <branch>] [--findings N]
           record that <id> reviewed the current content.
       python3 scripts/local_review.py check [--target <branch>]
           exit 0 covered, 1 not covered, 2 could not run.
       python3 scripts/local_review.py run --backend coderabbit-cli [--target <branch>]
           execute a headless backend and record its pass.

Exit codes are three-valued on purpose, per AGENTS.md "Report an execution boundary separately
from a check finding". 0 and 1 are findings the caller acts on. 2 means the check itself did not
run (no git repository, an unresolvable target ref, a receipt that cannot be read off disk, a
missing backend binary), which a gate must not silently read as either answer. Every unexpected
failure reports 2 as well, since a crash is the check not having run rather than a verdict of not
covered. `status` reports rather than gating, so it is 0 whether or not the content is covered,
and 2 only when it could not run at all.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

RECEIPT_VERSION = 1
RECEIPT_NAME = "local-review-receipt.json"
# Kept in the git directory rather than the working tree, so staging the tree does not stage these.
STAGING_INDEX_PREFIX = "local-review-index."
STAGING_OBJECTS_PREFIX = "local-review-objects."
DEFAULT_TARGET = "develop"

EXIT_COVERED = 0
EXIT_NOT_COVERED = 1
EXIT_CANNOT_RUN = 2

# Bounded so a hung git or CLI fails the capture point loudly instead of hanging a push forever.
# Staging the whole working tree is the slowest call here, which is why the git bound is generous.
GIT_TIMEOUT = 300
BACKEND_TIMEOUT = 900

# `headless` says whether a hook can run this backend with no agent session attached.
# An agent-driven one is recorded by the session that ran it, since no script can spawn an agent.
BACKENDS = {
    "agent-skill": {"headless": False, "why": "the local-strict-review subagent pass"},
    "coderabbit-cli": {"headless": True, "why": "coderabbit review --agent, structured JSON"},
}


class CannotRun(Exception):
    """The check could not be performed, as distinct from being performed and failing.

    Raised for an execution boundary the caller must not read as a verdict: no git repository, a
    target ref that does not resolve, a receipt that cannot be read off disk, a backend binary
    that is not installed.
    """


# Cleared from every call's environment unless that call sets them itself.
# A git hook runs with GIT_INDEX_FILE pointing at the commit it is gating, measured on a real pre-commit hook.
# A partial commit points it at a pending index rather than the real one.
# Inheriting that would silently read a different index than the one this branch would push.
INHERITED_REDIRECTS = ("GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def git(*args: str, root: Path | None = None, env: dict[str, str] | None = None) -> str:
    """Run a git command from `root` and return its stdout, raising CannotRun when git fails.

    Every call names its working directory rather than inheriting the process's own, because git
    reports paths relative to different places depending on the subcommand, so running two of them
    from different directories silently keys a path under a name that does not resolve.

    Decoding is UTF-8 with surrogateescape rather than the locale's encoding, so a path holding a
    non-ASCII byte round-trips to the same name on disk instead of decoding into a name that
    misses. `core.quotePath=false` stops git escaping those bytes before they are ever decoded.
    """
    run_env = {k: v for k, v in os.environ.items() if k not in INHERITED_REDIRECTS}
    run_env.update(env or {})
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=str(root) if root else None,
            env=run_env,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise CannotRun(f"git {' '.join(args)} could not be run ({e})") from e
    if proc.returncode != 0:
        raise CannotRun(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root() -> Path:
    """The working tree this invocation belongs to."""
    return Path(git("rev-parse", "--show-toplevel").strip())


def git_dir(root: Path) -> Path:
    """This worktree's own administrative directory.

    `--absolute-git-dir` resolves per worktree for a linked worktree, so two tasks in two worktrees
    of one repository never share a receipt or a staging index.
    """
    return Path(git("rev-parse", "--absolute-git-dir", root=root).strip())


def objects_dir(root: Path) -> Path:
    """The object store this worktree actually reads and writes.

    Asked of git rather than joined onto the git directory. A linked worktree's own git directory
    holds no `objects` at all, since the store lives in the common directory shared with every
    other worktree, and the fleet runs every task in a linked worktree. Joining the path by hand
    names a directory that does not exist there, which silently attaches nothing as an alternate.
    """
    return Path(
        git("rev-parse", "--path-format=absolute", "--git-path", "objects", root=root).strip()
    )


def receipt_path(root: Path) -> Path:
    """Where this worktree's receipt lives.

    Inside the git directory rather than the working tree, so the receipt can never be committed
    by accident, is never picked up as untracked content, and needs no .gitignore entry.
    """
    return git_dir(root) / RECEIPT_NAME


def target_ref(target: str) -> str:
    """The remote-tracking ref a target name means.

    A bare name is the ordinary fleet case and resolves under `origin`. A value already carrying a
    slash is used as written, so a fork-based flow can name an upstream remote's branch rather
    than being told the engine understands only one remote.
    """
    return target if "/" in target else f"origin/{target}"


def merge_base(target: str, root: Path) -> str:
    """The fork point this branch's diff is measured from.

    A merge-base is a fork point rather than a tip, so fetching a newer target does not move it.
    """
    ref = target_ref(target)
    try:
        git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", root=root)
    except CannotRun as e:
        raise CannotRun(
            f"{ref} does not resolve in this checkout, so the review scope cannot be determined"
        ) from e
    return git("merge-base", ref, "HEAD", root=root).strip()


def _zsplit(out: str) -> list[str]:
    """The non-empty records of a NUL-delimited git output."""
    return [f for f in out.split("\0") if f]


def _ls_files_states(out: str) -> dict[str, set[str]]:
    """Parse `ls-files -s` into path to the set of mode-and-blob states it holds.

    An unmerged path emits one record per stage, so the states are collected into a set rather
    than assigned. Keeping only the last record would silently reduce a conflicted index to one
    side of the conflict, and a pass recorded mid-conflict would then cover the resolution.

    The stage number is part of the state whenever it is not the ordinary zero. This is defense
    rather than a fix for a demonstrated case: a review argued that two stages of one conflict can
    carry an identical mode and object, which dropping the stage would collapse into a state
    reading as no conflict, but no construction produced it. Where our side's content equals the
    base, git records no modification and applies the other side's delete cleanly rather than
    conflicting, checked against a delete opposite an untouched file, an identical rewrite, and a
    mode-only change. The stage costs nothing to carry, so it stays.
    """
    states: dict[str, set[str]] = {}
    for record in _zsplit(out):
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) >= 3 and path:
            stage = f"{fields[2]}:" if fields[2] != "0" else ""
            states.setdefault(path, set()).add(f"{stage}{fields[0]}:{fields[1]}")
    return states


def base_states(base: str, root: Path) -> dict[str, str]:
    """Every path in the merge-base commit, as its mode-and-blob."""
    states = {}
    for record in _zsplit(git("ls-tree", "-r", "-z", base, root=root)):
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) >= 3 and path:
            states[path] = f"{fields[0]}:{fields[2]}"
    return states


def index_states(root: Path) -> dict[str, set[str]]:
    """Every path in the real index, with every stage it holds.

    The whole index is listed rather than a path list, which also keeps the call clear of the
    argument-length ceiling a branch touching tens of thousands of paths would otherwise hit.
    """
    return _ls_files_states(git("ls-files", "-s", "-z", root=root))


def alternate_entry(path: Path) -> str:
    """One entry for GIT_ALTERNATE_OBJECT_DIRECTORIES, quoted where it has to be.

    The variable holds a list separated by the platform's path separator, so a repository checked
    out under a path containing one would otherwise split into two directories that do not exist.
    Git reads a double-quoted entry as a single path.
    """
    text = str(path)
    return f'"{text}"' if os.pathsep in text else text


def worktree_states(root: Path) -> dict[str, set[str]]:
    """Every path as git would stage it right now, read by actually staging it.

    A throwaway index is seeded from the real one and `git add -A` is run against that, so git
    computes each blob and mode exactly as a real `git add` would, with every filter, attribute,
    and `core.fileMode` setting applied. Seeding from the real index rather than starting empty
    keeps git's cached stat information, so the scan is a refresh rather than a full re-hash.

    Staging writes objects, and those writes are redirected into a throwaway object directory with
    the real store attached as an alternate, so git can still read every existing object, and
    every alternate that store itself chains to, while writing none of its own. Without that redirection this read would permanently deposit the content of every
    untracked file into the repository, an unignored `.env` or key file among them, recoverable
    long afterwards from a command the caller had every reason to believe only looked.

    Both throwaway paths carry a unique name and live in the git directory. A fixed name would let
    two invocations in one worktree delete each other's index mid-read, and a name under the
    working tree would be staged as untracked content by the very command being run.

    One requirement this places on the repository: a `clean` filter has to be deterministic, since
    the same content is staged once when a pass is recorded and again when it is checked. A filter
    embedding a timestamp would move the key between two invocations that changed nothing.
    """
    gd = git_dir(root)
    real = gd / "index"
    fd, staging = tempfile.mkstemp(dir=str(gd), prefix=STAGING_INDEX_PREFIX)
    os.close(fd)
    objects = tempfile.mkdtemp(dir=str(gd), prefix=STAGING_OBJECTS_PREFIX)
    try:
        if real.is_file():
            shutil.copyfile(real, staging)
        env = {
            "GIT_INDEX_FILE": staging,
            "GIT_OBJECT_DIRECTORY": objects,
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternate_entry(objects_dir(root)),
        }
        git("add", "-A", "--", ".", root=root, env=env)
        return _ls_files_states(git("ls-files", "-s", "-z", root=root, env=env))
    except OSError as e:
        raise CannotRun(f"the staging index could not be prepared ({e})") from e
    finally:
        Path(staging).unlink(missing_ok=True)
        Path(staging + ".lock").unlink(missing_ok=True)
        shutil.rmtree(objects, ignore_errors=True)


def state_mark(index: set[str], work: set[str]) -> str:
    """One path's state, with the working tree's side named separately from the index's.

    A missing index entry contributes nothing rather than a state of its own, which is what keeps
    `git add` invisible to the key: an untracked file, and that same file staged unchanged, both
    read as the working tree's state alone.

    The index is named only where it actually disagrees with the working tree. Naming it that way
    rather than merging both sides into one set is what tells staging A over a working tree of B
    apart from the reverse, which a plain union renders identically while `git commit` pushes a
    different one of the two.
    """
    mark = "w:" + ",".join(sorted(work)) if work else "w:absent"
    if index and index != work:
        mark += "|i:" + ",".join(sorted(index))
    return mark


def fingerprints(base: str, root: Path) -> dict[str, str]:
    """Every changed path and the state it is in.

    A path is changed when either side, the real index or the staged working tree, disagrees with
    the merge base. Comparing states rather than reading a diff listing means renames, deletions,
    mode changes, and submodule bumps are all just a state that differs, with no per-case handling
    and no rename detection to disable.
    """
    at_base = base_states(base, root)
    at_index = index_states(root)
    at_work = worktree_states(root)
    out = {}
    for path in {*at_base, *at_index, *at_work}:
        base_state = {at_base[path]} if path in at_base else set()
        index = at_index.get(path, set())
        work = at_work.get(path, set())
        if index == work == base_state:
            continue
        out[path] = state_mark(index, work)
    return out


def content_digest(base: str, marks: dict[str, str]) -> str:
    """One digest over the merge base and every changed path's state.

    Paths are hashed in sorted order and a NUL follows every field, so two different path sets
    cannot collide by concatenating to the same string. The digest is derived from the repository
    on every call and never read back from the receipt, which is what makes a hand-edited receipt
    report stale rather than current.
    """
    h = hashlib.sha256()
    h.update(f"v{RECEIPT_VERSION}\0{base}\0".encode())
    for path in sorted(marks):
        h.update(path.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(marks[path].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


# Checked before a receipt is read, so a hand-edited file gives a verdict rather than a traceback.
# Shape rather than presence, because a partial write leaves keys missing.
# A hand edit instead leaves a key holding the wrong type, which a key-only check passes and then crashes on.
RECEIPT_SHAPE = {
    "receiptVersion": int,
    "target": str,
    "mergeBase": str,
    "contentDigest": str,
    "passes": list,
}


def receipt_problems(receipt: object) -> list[str]:
    """What makes this receipt unusable, in reading order, or an empty list where it is fine."""
    if not isinstance(receipt, dict):
        return [f"its root is {type(receipt).__name__} where an object is required"]
    out = []
    for key, want in RECEIPT_SHAPE.items():
        if key not in receipt:
            out.append(f"{key} is missing")
        elif not isinstance(receipt[key], want):
            out.append(f"{key} is {type(receipt[key]).__name__} where {want.__name__} is required")
    version = receipt.get("receiptVersion")
    # The version carries the format rather than the content, so a mismatch either way is unreadable.
    # A newer receipt holds fields this code does not read, and an older one lacks fields it does.
    # Carrying the field and never checking it is the version telling nobody anything.
    if isinstance(version, int) and version != RECEIPT_VERSION:
        out.append(f"receiptVersion is {version} where this script writes {RECEIPT_VERSION}")
    passes = receipt.get("passes")
    # Each pass is later read for its reviewer name and formatted into a message.
    # Reaching that line with a non-string name raises, and a raise is reported as a verdict.
    if isinstance(passes, list):
        for i, entry in enumerate(passes):
            if not isinstance(entry, dict):
                out.append(f"passes[{i}] is {type(entry).__name__} where an object is required")
            elif not isinstance(entry.get("reviewer"), str):
                out.append(f"passes[{i}].reviewer is missing or not a string")
    return out


def read_receipt(root: Path) -> tuple[dict | None, list[str]]:
    """The receipt on disk and what is wrong with it, or (None, []) where there is none.

    A receipt that cannot be read off disk at all, rather than one that parses into something
    unusable, is an execution boundary and raises. The difference matters: unusable content is a
    real answer about coverage, while an unreadable file means this check never ran.
    """
    path = receipt_path(root)
    try:
        if not path.is_file():
            return None, []
        raw = path.read_bytes()
    except OSError as e:
        raise CannotRun(f"the receipt at {path} could not be read ({e})") from e
    # Decoding belongs with parsing rather than with reading.
    # A non-UTF-8 receipt is unusable content, which is a verdict, not a file this process failed to read.
    # Decoding inside the block above would raise UnicodeDecodeError past both handlers and report the boundary code.
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    # ValueError rather than JSONDecodeError, since it also covers UnicodeDecodeError.
    except ValueError as e:
        return None, [f"the receipt does not parse ({e})"]
    problems = receipt_problems(data)
    return (data if not problems else None), problems


def current_state(target: str, root: Path) -> tuple[str, str, int]:
    """The merge base, digest, and changed-path count this invocation is talking about."""
    base = merge_base(target, root)
    marks = fingerprints(base, root)
    return base, content_digest(base, marks), len(marks)


def covering_passes(receipt: dict | None, base: str, digest: str, target: str) -> list[dict]:
    """The passes in `receipt` that actually cover the current content, which may be none.

    A pass covers the content only when the receipt agrees on all three of target, merge base, and
    digest. The target is checked because a review against one branch says nothing about the same
    branch retargeted at another, where the diff under review is a different one. It is compared
    as the ref it resolves to, so naming the same branch two ways is not a mismatch.
    """
    if not receipt:
        return []
    if target_ref(str(receipt.get("target"))) != target_ref(target):
        return []
    if receipt.get("mergeBase") != base or receipt.get("contentDigest") != digest:
        return []
    return [p for p in receipt.get("passes", []) if isinstance(p, dict) and p.get("reviewer")]


def held_lock(path: Path, timeout: float = 10.0) -> int:
    """Take an exclusive lock beside the receipt, or raise once waiting has gone on too long.

    Recording is a read, a merge, and a replace. The replace is atomic on its own, but two
    backends recording concurrently over one unchanged diff would both read a receipt without the
    other's pass and the second write would drop it, which is exactly the accumulation this file
    promises. The lock is what makes the promise true rather than usually true.
    """
    lock = Path(str(path) + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise CannotRun(f"another process holds {lock} and did not release it") from None
            time.sleep(0.05)
        except OSError as e:
            raise CannotRun(f"{lock} could not be created ({e})") from e


def write_pass(
    root: Path, target: str, base: str, digest: str, reviewer: str, findings: int | None
) -> dict:
    """Add one backend's pass to the receipt, dropping any pass that no longer covers the content.

    Passes accumulate so a sequence of backends over one unchanged diff builds up on one receipt.
    A pass recorded against a different digest is dropped rather than kept, since it reviewed
    content that no longer exists and would otherwise vote for a diff nobody read.

    The file is replaced rather than rewritten in place, so a reader never observes a half-written
    receipt and a process killed mid-write leaves the previous one intact.
    """
    path = receipt_path(root)
    fd_lock = held_lock(path)
    try:
        return _write_pass_locked(root, target, base, digest, reviewer, findings)
    finally:
        os.close(fd_lock)
        Path(str(path) + ".lock").unlink(missing_ok=True)


def _write_pass_locked(
    root: Path, target: str, base: str, digest: str, reviewer: str, findings: int | None
) -> dict:
    """The read, merge, and replace that `write_pass` holds the lock around."""
    receipt, _ = read_receipt(root)
    kept = [p for p in covering_passes(receipt, base, digest, target) if p["reviewer"] != reviewer]
    kept.append(
        {
            "reviewer": reviewer,
            "recordedUtc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "findings": findings,
        }
    )
    out = {
        "receiptVersion": RECEIPT_VERSION,
        "target": target,
        "mergeBase": base,
        "contentDigest": digest,
        "passes": kept,
    }
    path = receipt_path(root)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(out, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out


def run_coderabbit(base: str, root: Path) -> tuple[int, str]:
    """Run the CodeRabbit CLI over this branch's diff and return the findings count and an error.

    `--agent` is the CLI's documented structured-JSON mode for agent integrations. The base handed
    to it is the merge-base commit itself rather than the target's tip, because the two differ the
    moment the target moves: reviewing against the tip would cover the reverse of every commit the
    target gained since this branch forked, which is not what the receipt claims.

    The base is passed as `--base-commit`, which the CLI documents as taking a commit hash, rather
    than `--base`, which it documents as taking a branch name. Handing a merge-base sha to the
    branch flag was the earlier mistake here. `--include-untracked` is passed because the CLI
    excludes untracked files by default while this receipt's own scope includes them, so without
    it a pass would claim coverage of files the run never read.

    A completion event is required rather than assumed. A CLI build that does not understand
    `--agent`, or a run truncated part way, emits no findings and exits zero, which would
    otherwise be recorded as a clean review of content nothing read. A completion reporting
    `review_skipped` is refused for the same reason: it is the CLI saying it looked at nothing.

    None of this has been executed against a real CLI on this host, so the invocation follows the
    vendor's documented flag semantics rather than observed behavior, and the backend stays opt-in
    until someone runs it. A rate-limited or errored run
    likewise returns an error and the caller records no pass, since the CLI shares its hourly
    budget with this account's pull request reviews and a budget exhaustion must never look like a
    review.
    """
    # Only the documented binary name.
    # A bare `cr` is a common short name for unrelated tools.
    # Running whichever one is on PATH with a 900 second budget is not a fallback worth having.
    exe = shutil.which("coderabbit")
    if not exe:
        raise CannotRun("the coderabbit CLI is not installed, so this backend cannot run")
    try:
        proc = subprocess.run(
            [exe, "review", "--agent", "--base-commit", base, "--include-untracked"],
            cwd=str(root),
            # The same redirects the rest of the engine refuses to inherit.
            # Run from a hook that sets GIT_INDEX_FILE, the CLI would review the wrong tree.
            env={k: v for k, v in os.environ.items() if k not in INHERITED_REDIRECTS},
            capture_output=True,
            check=False,
            timeout=BACKEND_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise CannotRun(f"the coderabbit CLI could not be run ({e})") from e
    findings = 0
    complete = False
    error = ""
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type") or event.get("event")
        if kind == "finding":
            findings += 1
        elif kind == "complete":
            # A run that found nothing to look at also completes, reporting review_skipped.
            # Counting that as a review records a clean pass over content nothing read.
            # That is the same false clean the missing-completion case below guards against.
            if str(event.get("status") or "") == "review_skipped":
                error = "the CLI reported review_skipped, so no content was actually reviewed"
            else:
                complete = True
        elif kind == "error":
            error = str(event.get("message") or "the CLI reported an error event")
    if proc.returncode != 0 and not error:
        error = f"the CLI exited {proc.returncode}: {proc.stderr.strip() or 'no error text'}"
    if not error and not complete:
        error = "the CLI emitted no completion event, so the run cannot be treated as a review"
    return findings, error


def resolve_target(explicit: str | None) -> str:
    """The branch this work targets.

    The receipt is never consulted here. Reading the target back out of the receipt and then
    comparing the receipt against it is self-fulfilling, so a branch retargeted after a pass was
    recorded would verify against the branch it used to target and pass. Defaulting `record` from
    the receipt while `check` defaults to the fleet branch would also strand a receipt that can
    never cover, so both resolve the same way.

    An explicitly empty value is rejected rather than treated as absent, since a hook written as
    `--target "$VAR"` with the variable unset would otherwise silently gate the wrong branch.
    """
    if explicit is None:
        return DEFAULT_TARGET
    if not explicit.strip():
        raise CannotRun("--target was given an empty value, so the review scope is ambiguous")
    return explicit


def cmd_status(args: argparse.Namespace) -> int:
    root = repo_root()
    target = resolve_target(args.target)
    base, digest, changed = current_state(target, root)
    receipt, problems = read_receipt(root)
    passes = covering_passes(receipt, base, digest, target)
    print(
        json.dumps(
            {
                "target": target,
                "mergeBase": base,
                "contentDigest": digest,
                "changedPaths": changed,
                "covered": bool(passes),
                "reviewers": [p["reviewer"] for p in passes],
                "receiptProblems": problems,
            },
            indent=2,
        )
    )
    # Reports rather than gating, so a caller under `set -e` can run it whatever the answer is.
    return EXIT_COVERED


def cmd_record(args: argparse.Namespace) -> int:
    backend = BACKENDS.get(args.reviewer)
    if backend is None:
        known = ", ".join(sorted(BACKENDS))
        print(f"unknown reviewer '{args.reviewer}'. Known backends: {known}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if backend["headless"]:
        print(
            f"'{args.reviewer}' is headless, so it is recorded by running it rather than by hand."
            f" Use: run --backend {args.reviewer}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN
    root = repo_root()
    target = resolve_target(args.target)
    base, digest, changed = current_state(target, root)
    # The digest is read now, and the review finished some time earlier.
    # A format-on-save or a hook autofix in between would otherwise be stamped as reviewed.
    # That is content no reviewer saw, and attesting to it is the one claim this receipt makes.
    # The caller passes back what `status` reported before the pass ran.
    # A mismatch is the content having moved rather than an answer about coverage.
    if args.expect_digest and args.expect_digest != digest:
        print(
            "the content changed between the review and this record, so nothing was recorded.\n"
            f"  reviewed  {args.expect_digest[:12]}\n"
            f"  now       {digest[:12]}\n"
            "Re-run the review over the current content.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN
    out = write_pass(root, target, base, digest, args.reviewer, args.findings)
    reviewers = ", ".join(p["reviewer"] for p in out["passes"])
    print(
        f"recorded {args.reviewer} over {changed} changed path(s) against {target}"
        f" at {digest[:12]}. Passes now: {reviewers}"
    )
    return EXIT_COVERED


def cmd_check(args: argparse.Namespace) -> int:
    root = repo_root()
    target = resolve_target(args.target)
    base, digest, changed = current_state(target, root)
    receipt, problems = read_receipt(root)
    if covering_passes(receipt, base, digest, target):
        return EXIT_COVERED
    print(
        "No local review covers this branch's current content.\n"
        f"  target        {target}\n"
        f"  changed paths {changed}\n"
        f"  digest        {digest[:12]}",
        file=sys.stderr,
    )
    if receipt:
        print(
            f"  receipt       covers {receipt['target']} at {receipt['contentDigest'][:12]},"
            " which is not this content",
            file=sys.stderr,
        )
    for problem in problems:
        print(f"  receipt       {problem}", file=sys.stderr)
    print(
        "\nRun the local-strict-review pass over this diff, then record it:\n"
        "  python3 scripts/local_review.py record --reviewer agent-skill"
        f" --target {target} --findings <n>",
        file=sys.stderr,
    )
    return EXIT_NOT_COVERED


def cmd_run(args: argparse.Namespace) -> int:
    backend = BACKENDS.get(args.backend)
    if backend is None:
        known = ", ".join(sorted(k for k, v in BACKENDS.items() if v["headless"]))
        print(f"unknown backend '{args.backend}'. Headless backends: {known}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if not backend["headless"]:
        print(
            f"'{args.backend}' is {backend['why']}, which only a live agent session can run."
            " Run the pass, then record it with the record subcommand.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN
    root = repo_root()
    target = resolve_target(args.target)
    base, digest, _ = current_state(target, root)
    findings, error = run_coderabbit(base, root)
    if error:
        print(f"{args.backend} did not complete, so no pass was recorded: {error}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    # Re-read the content the backend actually finished against.
    # An edit during a run that takes minutes would otherwise be recorded as reviewed.
    after_base, after_digest, _ = current_state(target, root)
    if (after_base, after_digest) != (base, digest):
        print(
            f"{args.backend} finished but the content changed during the run,"
            " so no pass was recorded. Re-run it.",
            file=sys.stderr,
        )
        # The check ran and the honest answer is that nothing covers the content now.
        # Reporting the boundary code would let a gate that warns and continues on 2 wave it through.
        return EXIT_NOT_COVERED
    write_pass(root, target, base, digest, args.backend, findings)
    print(
        f"recorded {args.backend} against {target} at {digest[:12]}, {findings} finding(s)."
        " A pass records that a review ran, never that the content is clean."
    )
    return EXIT_COVERED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="what covers the current content, as JSON")
    p_status.set_defaults(func=cmd_status)

    p_record = sub.add_parser("record", help="record that a backend reviewed the current content")
    p_record.add_argument(
        "--reviewer", required=True, help=f"one of: {', '.join(sorted(BACKENDS))}"
    )
    p_record.add_argument("--findings", type=int, default=None, help="how many findings it raised")
    p_record.add_argument(
        "--expect-digest",
        default=None,
        help="the contentDigest the review was run against, refusing to record if it has moved",
    )
    p_record.set_defaults(func=cmd_record)

    p_check = sub.add_parser("check", help="exit 0 covered, 1 not covered, 2 could not run")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="execute a headless backend and record its pass")
    p_run.add_argument("--backend", required=True, help="a headless backend, e.g. coderabbit-cli")
    p_run.set_defaults(func=cmd_run)

    for p in (p_status, p_record, p_check, p_run):
        p.add_argument("--target", default=None, help=f"target branch (default {DEFAULT_TARGET})")

    args = parser.parse_args(argv)
    try:
        code = int(args.func(args))
        # Flushed inside the guarded region on purpose.
        # Piping into a reader that exits first leaves the payload sitting in a block buffer.
        # That buffer only fails during interpreter shutdown, which reports 120.
        # The handler below is never reached at all without this flush.
        sys.stdout.flush()
        return code
    except CannotRun as e:
        print(f"local_review: {e}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    # A reader that closes early, `| head -1` being the ordinary case, otherwise raises here.
    # It then raises again during the interpreter's own shutdown flush, which exits 120, outside the contract.
    except BrokenPipeError:
        # Redirected so the interpreter's own shutdown flush has somewhere to go.
        # Letting it raise again is what turns this into an exit outside the contract.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_CANNOT_RUN
    # A crash is the check not having run, so it reports the boundary code.
    # Falling through to the interpreter's own exit 1 would read as the not-covered verdict.
    except Exception as e:  # noqa: BLE001
        print(f"local_review: unexpected failure ({type(e).__name__}: {e})", file=sys.stderr)
        return EXIT_CANNOT_RUN


if __name__ == "__main__":
    sys.exit(main())
