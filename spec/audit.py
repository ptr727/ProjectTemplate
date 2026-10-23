#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py>=4", "pyyaml>=6"]
# ///
"""Live fleet audit: the deterministic subset of AUDIT.md, run from the hub (gh plus the libraries above).

Compares each cataloged registry repo against the ground truth in this repo - general settings
(repo-config/settings.json), branch rulesets (normalized diff vs the model's payloads), secret
names (spec/secrets.json; values are never read), baseline/per-type file presence and per-scope
Markdown section presence on the ground-truth branch (spec/files.json, spec/scope-model.md),
hub-hosted files a repo carries and should delete (git-tracked here and undeclared in the manifest,
triaged by spec/divergences.json), script file names and secret-file stems against CODESTYLE.md
"Script and Utility File Names", intent-staleness advisories (a carried intent file whose hub
canonical changed after the copy last did), and branch-model facts (main/develop existence, develop
behind main), and a fleet-wide membership check (every non-fork repo the registry owner has on
GitHub carries a registry/repos.json entry, and an 'archived' entry's status agrees with GitHub's
own archived flag. Owner-initiated: run it when
onboarding a repo, when drift is suspected, or before fleet-wide changes. Read-only - it never
modifies a target.

Findings: DEFECT (an applicable check fails outright), LETTER (a required file is absent - intent
unverified, judge per AUDIT.md section 7), DRIFT (non-breaking divergence, e.g. main-side
changes develop lacks, a stale secret, a registry field contradicting reality), ERROR (a gh call
failed, so the repo could not be fully audited). Exits non-zero when any repo has a DEFECT,
LETTER, or ERROR finding.

Usage: uv run --no-project --script spec/audit.py [RepoName ...] [--branch REF]   (default: every cataloged repo,
each read at its registry groundTruthBranch). --branch overrides that branch for the run, so a
convergence can be verified before it is promoted, without editing the registry.

A shallow hub clone cannot answer the stale-vs-modified classification or the intent staleness
advisory. A sweep then reports one ERROR per repo and exits non-zero.
`--issue` exits 2 with the error on stderr. The error says: Run `git fetch --unshallow origin` in that checkout.
"""

import argparse
import base64
import fnmatch
import functools
import hashlib
import itertools
import json
import pathlib
import posixpath
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import markdown_it
import validate  # sibling, import-safe (its main is guarded)
import yaml
from carried_text import extract_section, normalize, strip_sections
from git_env import git_location_cleared

ROOT = pathlib.Path(__file__).resolve().parent.parent

SETTINGS_KEYS = [
    "has_wiki",
    "has_projects",
    "allow_merge_commit",
    "allow_squash_merge",
    "allow_rebase_merge",
    "allow_auto_merge",
    "allow_update_branch",
    "delete_branch_on_merge",
]
# The bypass_actors field is deliberately absent from this list.
# Who may bypass a ruleset is a per-repository human decision taken in the UI.
# The configure.sh script in repo-config treats it that way, since apply writes the live list back unchanged and check reports it without asserting.
# Comparing it here would contradict that, and did.
# Once the payloads stopped declaring a bypass list, every repo whose live ruleset still had one reported a ruleset DEFECT.
# That was a field the fleet config had deliberately stopped managing.
# Two tools comparing one field under opposite policies is the defect, rather than the field's value.
RULESET_SUBSET = ["name", "target", "enforcement", "conditions", "rules"]
# Phrases that assert work is still outstanding, matched against a registry driftNote.
# Deliberately specific, so a note recording a permanent deviation must not match.
# An example of one that must not match is a note reading that there is no get-version-task and validate-task is relied on instead.
PENDING_MARKERS = ["pending", "not yet", "owed", "todo", "still", "behind", "missing", "absent"]


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


@functools.cache
def hub_tracked(rev=None):
    """The hub's own tracked paths at the resolved `main` commit, which is what "hub-side" means
    for the hub-only comparison.

    `git ls-tree -r` at that commit rather than `git ls-files` against ROOT's checked-out index
    (a filesystem walk is avoided too, since it would pick up __pycache__ and a local .venv and
    make the result depend on working-tree state), so a path present on `main` but absent on
    `develop`, or the reverse, is not silently missed or falsely added.
    Filtered to regular-file modes (100644, 100755) the same
    way `_git_revisions()` is: a directory, a symlink, or a submodule gitlink has no file content
    to compare, and every caller here assumes a plain file at each returned path. `-z` NUL-delimits
    the output so an unusual path is not C-quoted, which would otherwise return an escaped string
    that matches nothing a caller compares it against.

    `rev` defaults to `_hub_main_rev()`. The --selftest fixture passes an explicit `rev` to check
    against ROOT's own real tracked files without a network fetch, keeping the offline engine
    self-test offline.

    A non-zero exit raises rather than returning an empty set, which would read as "the hub tracks
    nothing" and silently clear every hub-only finding.
    """
    walk_rev = _hub_main_rev() if rev is None else rev
    # No text=True: -z's pathnames are raw bytes, and locale decoding here would crash on an invalid byte before the NUL-split below ever runs.
    # Decode each record with a fixed utf-8/surrogateescape policy instead of os.fsdecode(), whose error handler is platform-dependent (surrogatepass on Windows) and still crashes on an arbitrary invalid byte there.
    r = subprocess.run(
        ["git", "ls-tree", "-r", "-z", walk_rev],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        stderr = r.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git ls-tree -r {walk_rev} failed in {ROOT}: {stderr or 'non-zero exit'}"
        )
    paths = set()
    for record in r.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode = meta.split(None, 1)[0] if meta else None
        if mode in (b"100644", b"100755"):
            paths.add(path.decode("utf-8", errors="surrogateescape"))
    return frozenset(paths)


def hub_only_paths(spec, rev=None):
    """Hub-tracked paths the manifest does not declare, so a downstream copy is hub-hosted content rather than a carry.

    This is the deletion detector: the manifest says what a repo carries, so a file the hub tracks and the
    manifest omits is the hub's own, and a downstream copy of one is drift whose remedy is a deletion.
    Deriving it this way rather than from a hand-kept retirement list means a file dropped from the manifest
    starts being detected on the next run, with no second place to remember to edit.

    A hit is a candidate rather than a verdict, because the match is on path alone.
    A repo whose own content sits at a path the hub also uses matches without carrying anything of the hub's,
    which the first fleet run showed twice: a KiCad tooling doc at scripts/README.md, and per-repo formatting
    hooks at .husky/pre-commit.
    So only a `retire` disposition in spec/divergences.json asserts a deletion, and an untriaged hit asks for
    the file to be read.

    `rev` is passed through to `hub_tracked()`. See its docstring.
    """
    declared = {e["path"] for e in spec["files"]["baseline"]}
    tree_paths = set()
    for declaration in spec["files"].get("trees", []):
        root = declaration["source"].rstrip("/") + "/"
        tree_paths.update(
            path
            for path in hub_tracked(rev)
            if path.startswith(root)
            and tree_path_included(path.removeprefix(root), declaration["include"])
        )
    return hub_tracked(rev) - declared - tree_paths


def gap_dispositions(spec):
    """The curated ledger's manifest-gap dispositions, as path -> (disposition, reason).

    A gap the ledger triages is judged by its disposition rather than reported raw, so an accepted one
    (every repo owns its LICENSE) is not a finding and a retired one names the deletion.
    """
    out = {}
    for g in spec.get("divergences", {}).get("gaps", []):
        if (
            isinstance(g, dict)
            and isinstance(g.get("path"), str)
            and isinstance(g.get("disposition"), str)
        ):
            out[g["path"]] = (g["disposition"], g.get("reason", ""))
    return out


def repo_tree_blobs(slug, ground_head):
    """Every blob path on a repo branch mapped to its object SHA and file mode, or None when it cannot be read in full.

    The trees endpoint takes a tree sha rather than a ref name, so the branch payload already read is
    resolved to its tree instead of passing the branch name, which 404s.
    A truncated response returns None rather than a partial set, since a partial tree cannot distinguish a
    file the repo does not carry from one the response stopped short of.
    """
    sha = (((ground_head or {}).get("commit") or {}).get("commit") or {}).get("tree", {}).get("sha")
    if not sha:
        return None
    tree = gh(f"repos/{slug}/git/trees/{sha}?recursive=1", ok404=True)
    if not tree or "tree" not in tree or tree.get("truncated"):
        return None
    return {
        n["path"]: (n.get("sha"), n.get("mode")) for n in tree["tree"] if n.get("type") == "blob"
    }


def blob_field(blobs, index):
    """One field of each repo_tree_blobs() entry, 0 for the SHA and 1 for the mode, or None for None."""
    return None if blobs is None else {path: entry[index] for path, entry in blobs.items()}


def repo_tree_entries(slug, ground_head):
    """Every blob path and object SHA on a repo branch, or None when it cannot be read in full."""
    return blob_field(repo_tree_blobs(slug, ground_head), 0)


def repo_tree(slug, ground_head):
    entries = repo_tree_entries(slug, ground_head)
    return None if entries is None else set(entries)


def in_test_directory(path):
    """Whether any directory segment of path names a test directory, compared case-insensitively."""
    return any(segment.lower() in ("test", "tests") for segment in path.split("/")[:-1])


def is_python_test_module(path):
    """Whether path is a module pytest discovers by name, wherever in the tree it sits."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_csharp_test_project(path):
    """Whether path is a C# project either named or located as a test project.

    Read two ways because lowercasing the whole basename would claim Contest.csproj and
    Latest.csproj, which carry those letters inside a word rather than as a name segment.
    """
    if not path.endswith(".csproj"):
        return False
    name = path.rsplit("/", 1)[-1]
    segments = name[: -len(".csproj")].lower().split(".")
    return (
        "Test" in name
        or any(segment in ("test", "tests") for segment in segments)
        or in_test_directory(path)
    )


def coverage_candidate_types(types, repo_profiles):
    """The declared types whose profile leaves them owing coverage at all, before tests are looked for.

    Python's lint-only profile still runs its suite under coverage, so only a lint-only language other
    than Python drops out here, the type model holding coverage N/A for it.
    """
    profiles = repo_profiles if isinstance(repo_profiles, dict) else {}
    return [t for t in types if t == "python" or profiles.get(t) != "lint-only"]


def coverage_claiming_types(types, type_mechanisms, tree):
    """The declared types that owe Codecov coverage, meaning the CODECOV_TOKEN secret and codecov.yml.

    A type owes coverage when the fleet maps it to the codecov mechanism and the repo carries tests for
    it, `types` having already passed coverage_candidate_types(). The test condition is what keeps a package-only build repo, a library
    whose tests live elsewhere or are not yet written, from being told to store a token and commit a
    codecov.yml whose statuses gate a report its pipeline never produces.

    Each detector is deliberately broader than the hub validator's own guard, which keys its C# leg on
    `**/*Tests*.csproj` and its Python leg on a `tests/` directory in each Python directory it gates.
    D1.6 owes coverage to every repo with tests, and a repo the hub leg does not reach meets it through
    its own workflows instead, so a detector matching the guard exactly would drop the claim for a repo
    whose suite sits in `test/` or whose project is named `App.UnitTest.csproj`. Every uncertainty here
    therefore resolves toward keeping the claim, since a claim kept wrongly surfaces as a finding a
    reader can dismiss and a claim dropped wrongly is a silent clean report on a repo nobody measured.

    `tree` is the repo's blob path set, or None when it could not be read in full. On None the test
    question is unanswerable, so every candidate type keeps its claim, for that same reason. A type
    mapped to codecov with no detector here keeps its claim too.
    """
    detectors = {
        "csharp": lambda paths: any(is_csharp_test_project(p) for p in paths),
        "python": lambda paths: any(
            (p.endswith(".py") and in_test_directory(p)) or is_python_test_module(p) for p in paths
        ),
    }
    claiming = []
    for name in types:
        if type_mechanisms.get(name) != "codecov":
            continue
        detect = detectors.get(name)
        if tree is None or detect is None or detect(tree):
            claiming.append(name)
    return claiming


def python_directories_of(entry):
    """The registry's declared `pythonDirectories` for entry, or [] where none is declared.

    Read defensively (a non-list, or a non-string element, drops rather than raises) since a malformed
    declaration is spec/validate.py's own finding, and this audit gate must not crash mid-run on a
    registry CI already gates on push.
    """
    dirs = entry.get("pythonDirectories")
    return [d for d in dirs if isinstance(d, str)] if isinstance(dirs, list) else []


def python_directory_covers(directory, path):
    """Mirrors .github/actions/python-directories/python_directories.py's covers(): '.' covers every
    path, and any other declared directory covers only its own subtree."""
    return directory == "." or path.startswith(directory + "/")


def expected_python_directories(entry, tree):
    """The directories a caller's `python-directories` declaration is expected to resolve to.

    Mirrors python_directories.py's resolve() fallback for a caller declaring none: the registry's own
    `pythonDirectories` where declared, else the root where the tree carries a root pyproject.toml, else
    no directory at all. `tree` is the repo's blob path set, or None when it could not be read in full.
    """
    declared = python_directories_of(entry)
    if declared:
        return declared
    if tree is not None and "pyproject.toml" in tree:
        return ["."]
    return []


# The catalog's hook snippets carry this helper into repos of every language, so it is fleet tooling rather than the repo's own Python.
# A repo that copied it before the rename keeps the hyphenated name until it is next resynced.
CARRIED_PYTHON_HELPERS = {"hub_fetch_run.py", "hub-fetch-run.py"}


def declared_types(entry):
    """The string elements of the registry entry's `types` list, or an empty list where `types` is not a list.

    A malformed entry therefore reads as untyped, or as typed by its string elements alone, rather than
    aborting the run.
    """
    types = entry.get("types")
    return [t for t in types if isinstance(t, str)] if isinstance(types, list) else []


def is_repo_python(path):
    return path.endswith(".py") and posixpath.basename(path) not in CARRIED_PYTHON_HELPERS


def notes_naming(entry, cid):
    """The registry driftNotes naming check id `cid` in its parenthesized form."""
    return [n for n in entry.get("driftNotes", []) if f"({cid})" in n]


def quote_note(note):
    """A driftNote quoted for a finding, cut at 70 characters."""
    return f'"{note[:70]}{"..." if len(note) > 70 else ""}"'


def python_type_findings(entry, tree):
    """The discovery advisory for tracked `.py` files while the registry's `types` omits 'python'.

    Advisory rather than a defect, per spec/type-model.md's undeclared-but-detected row: the repo
    declares the type, or suppresses the advisory with a driftNote naming
    (python.directories.declared) and its reason, for vendored or incidental Python. `tree` is the
    repo's blob path set, or None when it could not be read in full, in which case Python's presence
    is unanswerable and nothing is reported.
    """
    if tree is None or "python" in declared_types(entry):
        return []
    suppressed = bool(notes_naming(entry, "python.directories.declared"))
    present = any(is_repo_python(p) for p in tree)
    # The note-freshness check skips this note on a repo without the type, so its staleness is reported here, where the tree is.
    if suppressed and not present:
        return [
            (
                "DRIFT",
                (
                    "registry: a driftNote suppresses the Python advisory (python.directories.declared), "
                    "but the tree carries no .py file. Delete the note."
                ),
            )
        ]
    if suppressed:
        return []
    if present:
        return [
            (
                "DRIFT",
                (
                    "registry: tracked .py file(s) present on the tree but types omits 'python'. "
                    "Declare the type, or record a driftNote naming (python.directories.declared) "
                    "with the reason Python is not tracked."
                ),
            )
        ]
    return []


def python_undeclared_root_findings(entry, tree):
    """DRIFT where a registry declaring no Python directory leaves a root suite no gate runs.

    The validator's root default runs a root tests/ suite only beside a root uv.lock or
    requirements*.txt, the gate it ran before directories could be declared. A root suite with
    neither runs only once a lint-only root is declared or a [project] root gains a manifest, and
    the blob paths alone cannot say which the root is. Meanwhile coverage_claiming_types() still
    claims coverage for it. `tree` is the repo's blob path set, or None when it could not be read
    in full, in which case nothing is reported.
    """
    if tree is None or "python" not in declared_types(entry):
        return []
    if python_directories_of(entry) or "pyproject.toml" not in tree:
        return []
    if not any(p.startswith("tests/") and p.endswith(".py") for p in tree):
        return []
    if "uv.lock" in tree or any(
        p.startswith("requirements") and p.endswith(".txt") and "/" not in p for p in tree
    ):
        return []
    return [
        (
            "DRIFT",
            (
                "python-directories: the root carries a tests/ suite with no uv.lock or "
                "requirements*.txt, which the undeclared root default never runs. A lint-only root "
                "declares '.' in the validator's python-directories input and the registry's "
                "pythonDirectories, and a root whose pyproject.toml declares [project] adds a "
                "uv.lock or requirements*.txt instead."
            ),
        )
    ]


def python_directory_coverage_findings(entry, tree):
    """DRIFT naming tracked `.py` files that sit outside every directory expected_python_directories()
    expects, mirroring python_directories.py's own uncovered() but measured against the registry's
    declaration rather than a caller's own runtime one, so a registry that stops matching what a repo's
    callers actually pass is caught here rather than only in each caller's own warning annotation.

    `tree` is the repo's blob path set, or None when it could not be read in full, in which case coverage
    is unanswerable and nothing is reported. The finding names the count and up to 5 example paths, since
    a whole tree of them would bury the message the same way python_directories.py's own warning caps at
    UNCOVERED_SHOWN rather than listing every path.
    """
    if tree is None or "python" not in declared_types(entry):
        return []
    directories = expected_python_directories(entry, tree)
    missed = sorted(
        p
        for p in tree
        if is_repo_python(p) and not any(python_directory_covers(d, p) for d in directories)
    )
    if not missed:
        return []
    return [
        (
            "DRIFT",
            (
                f"python-directories: {len(missed)} tracked .py file(s) sit outside every expected "
                f"directory ({', '.join(directories) or 'none'}): {capped_paths(missed)}"
            ),
        )
    ]


PYTHON_FILE_STEM = re.compile(r"[a-z0-9_]+")
HYPHENATED_FILE_STEM = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
HYPHENATED_SCRIPT_EXTENSIONS = (".sh", ".ps1", ".cmd", ".bat")
PESTER_SUFFIX = ".Tests.ps1"
SECRET_FILE_STEM = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
SECRETS_DIRECTORY_EXEMPT = {"README.md", ".gitignore", ".gitkeep"}
PATHS_SHOWN = 5


def capped_paths(paths):
    shown = ", ".join(paths[:PATHS_SHOWN])
    more = f" and {len(paths) - PATHS_SHOWN} more" if len(paths) > PATHS_SHOWN else ""
    return f"{shown}{more}"


FILE_NAMING_ID = "filenaming.scripts"
CODE_SPAN = re.compile(r"`([^`]+)`")


def file_naming_findings(entry, blobs, read_text):
    """DRIFT for script file names that break CODESTYLE.md "Script and Utility File Names".

    `blobs` maps each blob path to its file mode. It is None when the tree could not be read in
    full. Nothing is reported then, since the run's one `tree:` finding names that case.
    `read_text` returns a path's text, or None where none is returned. It is called only for a
    non-conforming extensionless executable, whose shebang decides whether the rule covers it.

    A `.py` stem is lowercase snake_case. A `.sh`, `.ps1`, `.cmd`, or `.bat` stem is lowercase-
    hyphenated once Pester's `.Tests.ps1` suffix is set aside, and so is an extensionless
    executable's name. An extension is recognized whatever its case, so an upper-case one breaks.
    A name whose text after its last dot is none of those extensions is not judged, whatever its
    mode. So an executable dotfile with no such extension, and a dotted name such as `Run_Me.v2`,
    go unjudged.
    A snake_case launcher with an extension, beside a `.py` of the same stem in its own directory,
    keeps that stem. Three cases go to judgment rather than breaking. One is a launcher whose `.py`
    sits only in another directory. Another is an extensionless launcher beside its `.py`, which
    may be an installed command instead. The last is an extensionless executable with no shebang
    or readable text.

    A registry driftNote naming (filenaming.scripts) exempts each path it names as a code span, for a
    name a tool or platform fixes. One note therefore cannot silence a later break on another path.
    A named path the check would not report is a stale exemption, and so is a note naming no path.
    """
    if blobs is None:
        return []
    exempt_notes = notes_naming(entry, FILE_NAMING_ID)
    python_stems = {}
    for path in blobs:
        directory, name = posixpath.split(path)
        if name.endswith(".py"):
            python_stems.setdefault(name[: -len(".py")], set()).add(directory)
    broken = []
    undecided = []
    for path in sorted(blobs):
        directory, name = posixpath.split(path)
        stem, dot, extension = name.rpartition(".")
        if not dot:
            if blobs[path] != "100755":
                continue
            stem = name
        else:
            extension = f".{extension}"
            lowered = extension.lower()
            if lowered == ".py":
                if extension != lowered or not PYTHON_FILE_STEM.fullmatch(stem):
                    broken.append(path)
                continue
            if lowered not in HYPHENATED_SCRIPT_EXTENSIONS:
                continue
            if extension != lowered:
                broken.append(path)
                continue
            if name.endswith(PESTER_SUFFIX):
                stem = name[: -len(PESTER_SUFFIX)]
        if HYPHENATED_FILE_STEM.fullmatch(stem):
            continue
        wrapped = python_stems.get(stem, set()) if PYTHON_FILE_STEM.fullmatch(stem) else set()
        if directory in wrapped and dot:
            continue
        if directory in wrapped:
            undecided.append((path, "an extensionless launcher may be an installed command"))
            continue
        if wrapped:
            undecided.append((path, f"a {stem}.py sits only in another directory"))
            continue
        if dot:
            broken.append(path)
            continue
        text = read_text(path)
        if text is None:
            undecided.append((path, "unreadable, so its shebang is unknown"))
        elif text.startswith("#!"):
            broken.append(path)
        else:
            undecided.append((path, "an executable with no shebang"))
    rule = 'CODESTYLE.md "Script and Utility File Names"'
    findings = []
    reported = set(broken) | {path for path, _ in undecided}
    exempt = set()
    for note in exempt_notes:
        paths = CODE_SPAN.findall(note)
        quoted = quote_note(note)
        if not paths:
            findings.append(
                (
                    "DRIFT",
                    (
                        f"registry: a driftNote naming ({FILE_NAMING_ID}) names no path as a code "
                        f"span, so it exempts nothing. Name each exempt path, or delete the note: {quoted}"
                    ),
                )
            )
        stale = [p for p in paths if p not in reported]
        if stale:
            findings.append(
                (
                    "DRIFT",
                    (
                        f"registry: a driftNote naming ({FILE_NAMING_ID}) exempts {len(stale)} "
                        "path(s) the file-naming check does not report. Drop each from the note, and "
                        f"the note once it exempts none: {capped_paths(stale)}"
                    ),
                )
            )
        exempt.update(paths)
    broken = [p for p in broken if p not in exempt]
    undecided = [f"{path} ({why})" for path, why in undecided if path not in exempt]
    if broken:
        findings.append(
            (
                "DRIFT",
                (
                    f"file-naming: {len(broken)} file(s) break {rule}. Rename each, or where a "
                    f"tool or platform fixes the name, record a driftNote naming "
                    f"({FILE_NAMING_ID}) and the path as a code span: {capped_paths(broken)}"
                ),
            )
        )
    if undecided:
        findings.append(
            (
                "DRIFT",
                (
                    f"file-naming: {len(undecided)} file(s) the per-extension patterns cannot "
                    f"decide, judge each by hand against {rule}: {capped_paths(undecided)}"
                ),
            )
        )
    return findings


def secret_stem_findings(tree):
    """DRIFT naming each secret file in a repo-scoped `.secrets/` directory whose stem is not lowercase snake_case.

    A secret file sits directly in the directory, as its `**/.secrets/*` ignore rule reads it. The
    directory's own README.md, .gitignore, and .gitkeep are not secret files. The stem is the
    name before its first dot, so `mail_relay.env.example` reads as conforming. Real secret
    files are untracked, so their tracked `.example` files are what the tree shows of them. `tree` is
    the repo's blob path set, or None when it could not be read in full. Nothing is reported then,
    as in file_naming_findings().
    """
    if tree is None:
        return []
    broken = []
    for path in sorted(tree):
        segments = path.split("/")
        name = segments[-1]
        if segments[-2:-1] != [".secrets"] or name in SECRETS_DIRECTORY_EXEMPT:
            continue
        if not SECRET_FILE_STEM.fullmatch(name.split(".", 1)[0]):
            broken.append(path)
    if not broken:
        return []
    return [
        (
            "DRIFT",
            (
                f"runtime-secrets: {len(broken)} secret file(s) carry a stem that is not lowercase "
                f"snake_case: {capped_paths(broken)}"
            ),
        )
    ]


@functools.cache
def canonical_blob_sha(path):
    """The hub's git blob identity for path, from the same resolved `main` commit
    `_git_revisions()` and `_hub_main_rev()` walk, not from ROOT's checked-out working tree,
    which is not necessarily `main`. Raises OSError, matching
    a filesystem read's own contract, when path is absent from that commit or is not a regular
    file there.
    """
    rev = _hub_main_rev()
    # Read via git ls-tree, not git rev-parse <rev>:<path>, which resolves a directory to a tree object id just as readily as a file to a blob one, silently breaking the regular-file promise above.
    result = subprocess.run(
        ["git", "ls-tree", rev, "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if result.returncode != 0:
        raise OSError(f"{path} is unreadable from the hub's main at {rev}: {result.stderr.strip()}")
    entry = result.stdout.strip()
    if not entry:
        raise OSError(f"{path} is absent from the hub's main at {rev}")
    mode, _, sha = entry.partition("\t")[0].split()
    if mode not in ("100644", "100755"):
        raise OSError(f"{path} is not a regular file at {rev} (mode {mode})")
    return sha


def tree_path_included(path, patterns):
    return any(
        pattern == "**/*"
        or fnmatch.fnmatchcase(path, pattern)
        or pathlib.PurePosixPath(path).match(pattern)
        for pattern in patterns
    )


@functools.cache
def hub_name():
    """This repo's name on the remote, and whether it came from the remote.

    Read from origin rather than the checkout directory, which a differently-named clone, a fork, or a
    worktree without an origin would silently break. The caller announces the directory-name fallback:
    a silently degraded match is the fail-open case this check exists to prevent.
    """
    r = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rstrip("/").removesuffix(".git").split("/")[-1], True
    return ROOT.name, False


def hub_repo_name() -> str:
    return hub_name()[0]


def gh(path, ok404=False) -> Any:
    """GET a REST path via gh, returning parsed JSON, or None on a 404 (when ok404) or an empty response body.

    No --paginate: on object endpoints it concatenates page documents into unparseable JSON. Every
    list read here fits one page; callers pass per_page=100 where a default page could truncate.
    """
    r = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if r.returncode != 0:
        if ok404 and ("HTTP 404" in r.stderr or "Not Found" in r.stderr):
            return None
        raise RuntimeError(
            f"gh api {path}: {r.stderr.strip().splitlines()[-1] if r.stderr else 'failed'}"
        )
    return json.loads(r.stdout) if r.stdout.strip() else None


def owner_repos(owner):
    """Every non-fork repository the registry owner actually owns on GitHub, paginated by hand.

    /user/repos rather than /users/{owner}/repos: the latter returns only public repos unless gh
    is authenticated as that exact user, which would silently under-count a private repo the same
    way a missing registry entry does. Confirm the authenticated login matches owner first, so a
    mismatch is a loud error rather than a quietly wrong (and possibly cross-account) result.
    No --paginate, per the gh() docstring above. Page by hand until a page comes back short.

    This still assumes the credential gh runs as can see every repo the owner has: an ordinary
    `gh auth login` session does, but a fine-grained PAT scoped to "selected repositories" would
    return an incomplete list with no error and no signal in the response to detect that from, so
    the sweep would read as clean while some repos were never inspected. Run this as the owner's
    own account, not a repository-scoped credential.
    """
    me = gh("user")
    login = me.get("login") if isinstance(me, dict) else None
    # GitHub logins are case-insensitive.
    # Compare lowered, or a registry owner spelled with different casing than gh's own login reads as a mismatch when it is the same account.
    if (login or "").lower() != owner.lower():
        raise RuntimeError(
            f"gh is authenticated as '{login}', not registry owner '{owner}'. "
            "The membership check would query the wrong account's repos."
        )
    repos, page = [], 1
    while True:
        batch = gh(f"user/repos?affiliation=owner&per_page=100&page={page}")
        # An empty response body reads as None here, per the gh() docstring above.
        # `or []` would silently take that for a short page and stop the sweep, reporting the fleet clean while the rest of the repos were never actually inspected.
        # Fail loud instead, matching the ownership guard above.
        if batch is None:
            raise RuntimeError(f"GitHub returned an empty repository-list response (page {page})")
        if not isinstance(batch, list) or not all(isinstance(r, dict) for r in batch):
            raise RuntimeError(f"GitHub returned an unexpected repository-list shape (page {page})")
        repos.extend(r for r in batch if not r.get("fork"))
        if len(batch) < 100:
            return repos
        page += 1


def repo_identity(url):
    """owner/repo, lowercased, parsed from a github.com URL, or None if it does not parse.

    The canonical identity two repos are compared by, since a bare repo name collides across owners.
    GitHub's own full_name field is already this exact shape (case-preserved).
    A trailing .git is stripped so it still resolves to the same identity full_name would.
    A query string or fragment is rejected outright rather than folded into the repo name.

    spec/validate.py's github_identity() is the one parse, called rather than restated, since a byte-identical copy
    of that regex here is a second definition that only looks like one.

    The padding is trimmed before that call, deliberately and in one direction only. This function decides whether a
    live repo has an entry, so a padded url resolving to nothing here would report the repo as having no entry at all,
    a DEFECT naming the wrong problem. The gate refuses the padding itself, which is where that finding belongs, so
    audit reads the value as generously as it can and validate.py is what says no.
    """
    if not isinstance(url, str):
        return None
    slug = validate.github_identity(url.strip())
    return slug.lower() if slug is not None else None


def membership_findings(spec):
    """Fleet-wide: every non-fork repo the owner has on GitHub must have a registry entry.

    Nothing else in this file, or in spec/validate.py, ever looks past the registry to what
    actually exists, so a repo that never got an entry is invisible to every tool that reads it.
    The registry was treated as ground truth about existence, not just about conformance. Ownership only: a fork, or a repo the
    owner merely collaborates on, was never meant to carry a registry entry.

    A registry entry's status then says what, if anything, the rest of the audit owes the repo:
    'cataloged' is audited normally, 'backlog' awaits classification (existing behavior), and
    'archived'/'excluded' are known and out of scope by design: archived because GitHub itself
    says the repo takes no further work, excluded because exclusionReason records a human
    decision that it should not be audited. Both still require an entry, so the decision stays
    visible in the catalog instead of the repo just disappearing from it.
    """
    findings = []
    # A bare-name match would treat a same-named repo under a different owner as this one's entry.
    # Keyed by owner/repo instead, the shape GitHub's own full_name field already carries.
    # Two entries resolving to the same identity are a separate, validate.py-level defect.
    registry_by_identity = {}
    for r in spec["registry"]["repos"]:
        identity = repo_identity(r.get("url")) if isinstance(r, dict) else None
        if identity is not None:
            registry_by_identity[identity] = r
    for gh_repo in owner_repos(spec["registry"]["owner"]):
        entry = registry_by_identity.get(str(gh_repo.get("full_name", "")).strip().lower())
        if entry is None:
            findings.append(
                (
                    "DEFECT",
                    f"{gh_repo['full_name']}: exists on GitHub with no registry/repos.json entry",
                )
            )
            continue
        gh_archived = bool(gh_repo.get("archived"))
        reg_archived = entry.get("status") == "archived"
        if gh_archived and not reg_archived:
            msg = (
                f"{entry['name']}: archived on GitHub but registry status is "
                f"'{entry.get('status')}' (reconcile to 'archived')"
            )
            findings.append(("DRIFT", msg))
        elif reg_archived and not gh_archived:
            msg = (
                f"{entry['name']}: registry status is 'archived' but GitHub reports it "
                "unarchived (reconcile status, or verify it was not unarchived by mistake)"
            )
            findings.append(("DRIFT", msg))
    return findings


def docker_hub_description(slug):
    """The Docker Hub short description for a repo, or None if the image is genuinely absent (HTTP 404).

    The image name is taken as owner/repo lowercased, the fleet convention (`ptr727/PhotoCleaner` ->
    `ptr727/photocleaner`), so a repo whose image is named otherwise, or not yet pushed, 404s and is skipped
    rather than falsely flagged. A transient failure (timeout, network, non-404 status) is **raised**, not
    swallowed, so the caller surfaces "could not verify" instead of silently passing. Read-only, unauthenticated.
    """
    owner, repo = slug.split("/", 1)
    url = f"https://hub.docker.com/v2/repositories/{owner.lower()}/{repo.lower()}/"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8")).get("description")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def normalize_ruleset(payload):
    sub = {k: payload.get(k) for k in RULESET_SUBSET}
    if isinstance(sub.get("rules"), list):
        sub["rules"] = sorted(sub["rules"], key=lambda r: json.dumps(r, sort_keys=True))
    return json.dumps(sub, sort_keys=True)


def check_id_owner(spec, cid):
    """`(owning project type or None, whether spec/checks.json or spec/project-types.json defines the id)`."""
    types = spec.get("types")
    if not types:
        raise KeyError(
            "spec['types'] (spec/project-types.json) is required to resolve a driftNote check id"
        )
    for name, t in types.get("types", {}).items():
        if any(c.get("id") == cid for c in t.get("checks", [])):
            return name, True
    cross = (c for dim in types.get("crossCutting", {}).values() for c in dim.get("checks", []))
    known = any(c.get("id") == cid for c in cross)
    return None, known or any(c["id"] == cid for c in registered_checks())


def is_suppression_id(entry, cid):
    """Whether a driftNote naming `cid` suppresses a registered check's finding on this repo, rather than naming a check to judge."""
    check = next((c for c in registered_checks() if c["id"] == cid), {})
    if "suppression" not in check:
        return False
    ending_type = check["suppression"]["endsWithType"]
    return ending_type is None or ending_type not in declared_types(entry)


def driftnote_findings(entry, spec, open_count):
    """Freshness findings over one repo's registry driftNotes, given how many findings the audit already has.

    Neither shape below is gated on the rest of the audit being clean, and the gate that used to wrap both
    is the defect this replaces: one standing finding a repo cannot clear exempted its whole note list, so
    the repo with open findings, where a stale note is most likely, was the one never checked.

    A note naming a check id declares what would retire it, so it is surfaced on every run. What the audit
    cannot do is decide it, since no per-type check is mechanized, so the id is resolved against the
    catalog here and the check itself is left to the auditor. A pending-marker note is a prose claim that
    work is outstanding, so a clean audit contradicts it outright while an unclean audit only asks which of
    the open findings it means. Narrow markers keep a permanent-deviation note ("relies on validate-task")
    from tripping.
    """
    out = []
    for note in entry.get("driftNotes", []):
        quoted = quote_note(note)
        for cid in validate.CHECK_ID_RE.findall(note):
            if is_suppression_id(entry, cid):
                continue
            owner, known = check_id_owner(spec, cid)
            if not known:
                out.append(
                    (
                        "DRIFT",
                        f"registry: driftNote names check '{cid}', which neither spec/checks.json nor spec/project-types.json defines - fix the id or drop the note: {quoted}",
                    )
                )
            elif owner and owner not in declared_types(entry):
                out.append(
                    (
                        "DRIFT",
                        f"registry: driftNote names check '{cid}', whose type '{owner}' this repo does not declare: {quoted}",
                    )
                )
            else:
                out.append(
                    (
                        "DRIFT",
                        f"registry: driftNote names check '{cid}', which this audit does not evaluate by id (AUDIT.md section 4) - judge it by hand and delete the note once it passes: {quoted}",
                    )
                )
        marker = next(
            (w for w in PENDING_MARKERS if re.search(rf"\b{re.escape(w)}\b", note, re.IGNORECASE)),
            None,
        )
        if marker and not open_count:
            out.append(
                (
                    "DRIFT",
                    f"registry: driftNote says '{marker}' but the audit is clean - verify and reconcile: {quoted}",
                )
            )
        elif marker:
            out.append(
                (
                    "DRIFT",
                    f"registry: driftNote says '{marker}' while {open_count} finding(s) are open - confirm it describes one of them rather than closed work: {quoted}",
                )
            )
    return out


# The owner half of the slug repo_slug() answers with when it cannot parse an entry's url, plus the stand-in for a name it cannot use.
# Both carry an underscore, which GitHub allows in a repository name and not in an owner name.
# So the pair addresses no repository that can exist, while still naming the entry in every message the finding reaches.
UNRESOLVED_OWNER = "_unresolved"
UNNAMED_ENTRY = "_unnamed"


def repo_slug(entry):
    """The `<owner>/<repo>` every `repos/{slug}/...` read below addresses.

    Parsed by spec/validate.py's own url grammar rather than by taking the last two path segments, so what this
    addresses and what that gate accepted are the same value. Taking the segments kept a trailing `.git` that the
    gate strips for the identity, sending every read to `repos/<owner>/<repo>.git/...`.

    A url this module cannot parse never raises, because this module is not the gate. spec/validate.py refuses such
    a url, and nothing runs it before an audit: spec/fidelity_honesty.py and spec/workflow_reuse.py both call this on
    every entry with no handler, so raising would abort a whole fleet report over one malformed entry that instead
    404s and lands in the unreadable bucket beside its healthy siblings. The finding stays where it belongs, on the
    gate.

    Anything else answers with UNRESOLVED_OWNER and the entry's name, so every read 404s and the message names the
    entry that caused it. spec/workflow_reuse.py reaches this by building `{"name": hub_repo_name()}` as its own
    fallback when the hub has no registry entry and calling this on it.

    Each half of that sentinel answers a way a fallback slug has already addressed the wrong thing. It carries two
    segments because a one-segment value shifts every later path component up one: `repos/Fixture/git/trees/<sha>`
    reads owner `Fixture` and repository `git` rather than failing. Its owner segment carries an underscore, which
    GitHub does not allow in an owner name, so it cannot collide with a real owner. And a name that is nothing but
    dots is dropped rather than encoded, because GitHub decodes `%2e` and then normalizes the dot segment it becomes,
    so `repos/<owner>/../branches/main` would read `repos/branches/main`.

    Taking the url's last two path segments is what all of this replaced, and it produced a plausible slug rather
    than a failing one. `https://gitlab.test/owner/Repo` became `owner/Repo` and read that repository on github.com,
    and a `?` survived into the value, where `repos/<owner>/<repo>?tab=readme/branches/main` ends its path at
    `repos/<owner>/<repo>` and sends the rest as a query string.
    """
    slug = validate.github_identity(entry.get("url"))
    if slug is not None:
        return slug
    name = str(entry.get("name", ""))
    tail = urllib.parse.quote(name, safe="") if name.strip(".") else UNNAMED_ENTRY
    return f"{UNRESOLVED_OWNER}/{tail}"


def repo_selectors(entry, defaults):
    """The scope-selector set a files.json appliesTo is matched against (see spec/scope-model.md).

    The four namespaces - project types, workflowModel, releaseTrigger, consumerModel - are disjoint, so
    a flat token set is unambiguous. workflowModel and releaseTrigger resolve repo -> defaults -> fleet
    default (as configure.sh does). consumerModel has no fleet default - validate.py requires it on every
    cataloged repo, so a cataloged repo always contributes one.
    """
    sel = set(declared_types(entry))
    sel.add(entry.get("workflowModel") or defaults.get("workflowModel") or "release")
    sel.add(entry.get("releaseTrigger") or defaults.get("releaseTrigger") or "two-phase")
    # The consumerModel field has no defaults fallback, since the registry schema does not allow defaults.consumerModel and validate.py requires it on every cataloged repo.
    # The guard only shields a malformed non-cataloged entry.
    cm = entry.get("consumerModel")
    if cm:
        sel.add(cm)
    return sel


def applies(applies_to, sel):
    """True if an appliesTo selector applies to a repo's selector set. Disjunctive any-of, with `*` meaning all."""
    if applies_to == "*":
        return True
    tokens = applies_to if isinstance(applies_to, list) else [applies_to]
    return bool(set(tokens) & sel)


def _section_spec(elt):
    """Normalize a sections[] entry to (name, appliesTo, fidelity). A bare string is appliesTo `*`, intent."""
    if isinstance(elt, str):
        return elt, "*", "intent"
    return elt.get("name", ""), elt.get("appliesTo", "*"), elt.get("fidelity", "intent")


def required_sections(item, sel):
    """Section names to presence-check (heading grep) for this repo - the non-verbatim sections.

    A bare-string section is appliesTo `*`, intent; an object section carries its own selector and fidelity.
    A verbatim section is checked byte-for-byte instead (verbatim_sections), so it is excluded here to avoid a
    redundant presence finding. The entry's own appliesTo is assumed already matched by the caller.
    """
    out = []
    for elt in item.get("sections", []):
        name, sec, fid = _section_spec(elt)
        if name and fid != "verbatim" and applies(sec, sel):
            out.append(name)
    return out


def verbatim_sections(item, sel):
    """Section names marked fidelity verbatim for this repo - checked byte-for-byte against the hub canonical."""
    out = []
    for elt in item.get("sections", []):
        name, sec, fid = _section_spec(elt)
        if name and fid == "verbatim" and applies(sec, sel):
            out.append(name)
    return out


# Carried files scanned for a coordination reference (GOVERNANCE.md "Documentation Style Conventions").
TEMPLATE_REF_SCANNED = ("AGENTS.md", "GOVERNANCE.md", ".github/copilot-instructions.md")

# The undeclared-H2 scan reads the same set.
UNDECLARED_HEADING_SCANNED = TEMPLATE_REF_SCANNED

VERSION_LITERAL_SCANNED = validate.VERSION_LITERAL_SCANNED
VERSION_LITERAL = validate.VERSION_LITERAL


def version_literals_outside_verbatim(text, verbatim_names):
    """Every version literal or commit SHA in `text` outside its verbatim sections, sorted and deduplicated.

    A pin lives in the workflow or manifest that uses it, where Dependabot moves it, so a copy in prose is
    stale at the next bump and sends an agent to edit governance for a change that needed none.
    Verbatim sections are excised downstream for the reason template_ref_outside_verbatim gives: their
    bytes are the hub's, so a literal there is the hub's defect to fix once rather than each repo's. The
    hub's own run passes no sections, which is where that defect is caught.
    Fenced code is scanned too, since a pinned `uses:` line in a sample goes stale the same way.
    """
    return sorted(
        set(VERSION_LITERAL.findall(strip_sections(text, verbatim_names, keep_pins=True)))
    )


def undeclared_h2_headings(text, declared):
    """Level-two headings in `text` that `declared` does not name, folded and sorted."""
    h2s = {h.strip().lower() for level, h, _, _ in _atx_headings(text) if level == 2}
    return sorted(h2s - {d.strip().lower() for d in declared})


def template_ref_outside_verbatim(text, verbatim_names, hub_name):
    """True when `hub_name` appears in `text` outside every one of its verbatim sections.

    A verbatim section's bytes are the hub's canonical and are checked byte-for-byte elsewhere, so a hub
    reference inside one cannot be removed downstream: the repo would have to fail the verbatim check to
    clear this one. `AGENTS.md > Fleet Bootstrap` is the standing case, since naming the hub is that
    section's entire function - it is the byte-locked entry point stating where the canonical rules live,
    and a repo holding no current copy of anything else is exactly who reads it. Excising the verbatim
    regions before scanning keeps the check pointed at the prose a repo actually owns. A hub reference that
    reaches a verbatim section is the hub's defect to fix once in the canonical, never each repo's to clear.
    """
    return hub_name.lower() in strip_sections(text, verbatim_names).lower()


# CommonMark plus tables, with escapes kept as their own tokens so a line can be rebuilt from its source.
_MD = markdown_it.MarkdownIt("commonmark").enable("table").disable("text_join")


def _atx_headings(text):
    """`(level, raw text, first line, last line)` for each ATX heading, the form the region reader splits on."""
    tokens = _MD.parse(text)
    return [
        (int(t.tag[1]), tokens[i + 1].content, t.map[0], t.map[1])
        for i, t in enumerate(tokens)
        if t.type == "heading_open" and t.markup.startswith("#") and t.map
    ]


def references(text):
    """The reference definitions markdown-it reads from a document, keyed the way it resolves a link."""
    env = {}
    _MD.parse(text, env)
    return env.get("references", {})


def heading_texts(markdown):
    """Lowercased heading texts in a Markdown document, for case-insensitive section-presence matching."""
    return {h.strip().lower() for _, h, _, _ in _atx_headings(markdown)}


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_md_links(text, references=None):
    """`text` with each Markdown link reduced to its label, everything else kept as written.

    `references` is the document's reference table, so `[text][ref]` is a link only where `ref` is defined.
    """
    out = []
    for tok in _MD.parseInline(text, {"references": dict(references or {})})[0].children or []:
        if tok.type in ("text", "html_inline"):
            out.append(tok.content)
        elif tok.type == "code_inline":
            out.append(tok.markup + tok.content + tok.markup)
        elif tok.type == "softbreak":
            out.append("\n")
        elif tok.type not in ("link_open", "link_close"):
            out.append(tok.markup)
    return "".join(out)


def title_and_intro(text):
    """The H1 title text and the intro region between it and the first H2, HTML comments stripped."""
    lines = _HTML_COMMENT.sub("", normalize(text)).split("\n")
    heads = _atx_headings("\n".join(lines))
    first = next((h for h in heads if h[0] == 1), None)
    if first is None:
        return None, ""
    end = next((h[2] for h in heads if h[0] == 2 and h[2] > first[2]), len(lines))
    return first[1].strip(), "\n".join(ln.rstrip() for ln in lines[first[3] : end]).strip("\n")


def tagline(intro):
    """The first line of a README/HISTORY intro region - the one canonical short description.

    Per spec/readme-structure.md item 1, only this line carries the length and link-free rules and only this
    line mirrors to the About panel, the Docker Hub short description, and the HISTORY.md opening. Any further
    paragraph is free prose no mirror reads, so comparing the whole region would report a legitimate second
    paragraph as a mirror difference.
    """
    return intro.split("\n")[0]


# Text scans, since a parsed link keeps its target but not the reference name and position these checks read.
_MD_IMAGE_REF = re.compile(r"!\[[^\]]*\]\[([^\]]+)\]")
_MD_IMAGE_INLINE = re.compile(r"!\[[^\]]*\]\((\S+?)\)")
_LINK_DEF = re.compile(r"^\[([^\]]+)\]:\s*(\S+)", re.MULTILINE)
# A URI scheme, requiring two or more characters so a `C:` drive letter is not read as one.
# Every scheme a README actually carries (mailto, ftp, ssh, git, tel, data) is longer than that.
_URI_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]+:", re.IGNORECASE)


def unfenced_text(text):
    """`text` normalized with every fenced code block's lines removed, since markup in a sample is shown rather than used."""
    lines = normalize(text).split("\n")
    fenced = {
        i for t in _MD.parse("\n".join(lines)) if t.type == "fence" and t.map for i in range(*t.map)
    }
    return "\n".join(ln for i, ln in enumerate(lines) if i not in fenced)


def readme_region(text, heading):
    """extract_section against a comment-stripped copy of the document.

    extract_section matches a heading by its exact parsed text, so `## License <!-- omit from toc -->` is not
    the section named License and the lookup returns None. Four repos suffix every heading that way for the
    Markdown All in One extension, and reading them as sectionless reported twelve absent sub-sections that
    were present. Stripping first is confined to the README checks: extract_section itself must keep the
    comment, since the verbatim engine hashes a section's exact bytes.

    Fences are stripped here rather than at each caller. Every region the README checks read comes through
    this one function, so doing it at the root is what stops a code sample inside a section from being read
    as a badge, a tool row, or a definition by whichever caller forgot.
    """
    return extract_section(_HTML_COMMENT.sub("", unfenced_text(text)), heading)


def shield_endpoints(region, defs):
    """The image URLs a Markdown region renders, from both its `![alt][ref]` and its `![alt](url)` uses.

    Keyed on the endpoint rather than the alt text or the reference name, because those are captions. The
    fleet writes Release Status, Releases Build, Build Status, Workflow Status and Lint Build for one badge,
    and names its reference `last-commit-shield` and `lastcommit-shield` in the same breath, while the
    endpoint under img.shields.io is identical in every repo.

    Inline uses are resolved as well as reference ones. Reading references alone made an inline shield
    invisible rather than wrong, so a repo writing every badge inline, which the reference-link rule forbids
    for a separate reason, would have passed the shield check by carrying nothing the check could see.
    A `[ref]: url` definition line is not an image, so it is never counted as a use, which is what keeps a
    shield's own definition from reading as a second placement of it.
    """
    region = region or ""
    urls = [defs[m.group(1)] for m in _MD_IMAGE_REF.finditer(region) if m.group(1) in defs]
    return urls + [m.group(1) for m in _MD_IMAGE_INLINE.finditer(region)]


def shield_matches(url, shield):
    """True where a rendered URL is the shield the model describes, by endpoint plus its query discriminators."""
    return (
        shield["match"] in url
        and all(q in url for q in shield.get("requireQuery", []))
        and (shield.get("forbidQuery") is None or shield["forbidQuery"] not in url)
    )


def addressed_region(text, address):
    """The region a shield's `in` address names, as `Heading` or `Heading > Sub-heading`, or None if absent."""
    parts = [p.strip() for p in address.split(">")]
    body = readme_region(text, parts[0])
    if body is None or len(parts) == 1:
        return body
    lines = body.split("\n")
    h3s = [h for h in _atx_headings(body) if h[0] == 3]
    for i, (_, name, _, end) in enumerate(h3s):
        if name.strip().lower() == parts[1].lower():
            stop = h3s[i + 1][2] if i + 1 < len(h3s) else len(lines)
            return "\n".join(lines[end:stop])
    return None


def ordered_headings(markdown, level):
    """Heading texts at `level` in document order, HTML comments stripped, fenced blocks skipped."""
    return [
        _HTML_COMMENT.sub("", h).strip() for lv, h, _, _ in _atx_headings(markdown) if lv == level
    ]


def readme_section_findings(text, model, sel, public):
    """README section presence and order against spec/readme-sections.json.

    Headings the model does not name are dropped before the order comparison, so the roughly sixty genuinely
    repo-specific sections across the fleet sit anywhere without a finding - the model constrains the sections
    it declares and nothing else. A retired name still resolves to its canonical ordinal, so a repo that has
    not renamed yet is checked on order too rather than silently losing the check along with the name.
    """
    findings = []
    by_name = {s["name"].lower(): s for s in model["sections"]}
    retired = {r.lower(): s for s in model["sections"] for r in s.get("retiredNames", [])}
    heads = ordered_headings(text, 2)
    lower = [h.lower() for h in heads]
    present = set(lower)

    for h, hl in zip(heads, lower):
        if hl in retired:
            findings.append(
                (
                    "LETTER",
                    f"readme: section '{h}' uses a retired name - rename it to '{retired[hl]['name']}', which has no accepted aliases (spec/readme-structure.md)",
                )
            )

    for s in model["sections"]:
        req = s["required"]
        if req == "optional" or (req == "public" and not public):
            continue
        if {t.lower() for t in s.get("notApplicableTo", [])} & {t.lower() for t in sel}:
            continue
        if s["name"].lower() in present or any(
            r.lower() in present for r in s.get("retiredNames", [])
        ):
            continue
        why = " (a public repo; optional while private)" if req == "public" else ""
        findings.append(
            (
                "LETTER",
                f"readme: no `## {s['name']}` section{why} - it is a required section (spec/readme-structure.md)",
            )
        )

    seq = [
        (by_name.get(hl) or retired.get(hl), h)
        for h, hl in zip(heads, lower)
        if hl in by_name or hl in retired
    ]
    for (prev, prev_h), (cur, cur_h) in itertools.pairwise(seq):
        if cur["ordinal"] < prev["ordinal"]:
            findings.append(
                (
                    "LETTER",
                    f"readme: section '{cur_h}' follows '{prev_h}' but is ordered before it - the declared sections keep their relative order (spec/readme-structure.md)",
                )
            )

    last = next((s for s in model["sections"] if s.get("last")), None)
    if last and last["name"].lower() in present and lower[-1] != last["name"].lower():
        findings.append(
            (
                "LETTER",
                f"readme: `## {last['name']}` is not the last section, '{heads[-1]}' follows it - it closes the file, immediately before the link definitions (spec/readme-structure.md)",
            )
        )

    for s in model["sections"]:
        if s["name"].lower() not in present or not s.get("subsections"):
            continue
        have = {h.lower() for h in ordered_headings(readme_region(text, s["name"]) or "", 3)}
        for sub in s["subsections"]:
            if sub.lower() not in have:
                findings.append(
                    (
                        "LETTER",
                        f"readme: `## {s['name']}` carries no `### {sub}` sub-section (spec/readme-structure.md)",
                    )
                )
    return findings


def distribution_prefixes(model, slug):
    """The URL prefixes that make a link this repo's own, from the model's single declaration of ownership."""
    owner = slug.split("/")[0]
    return tuple(
        p.replace("{slug}", slug).replace("{owner}", owner).lower()
        for p in model.get("distribution", {}).get("urlPrefixes", [])
    )


def link_kind(url, slug, is_rendered=False, prefixes=()):
    """Which linkGroups kind a reference definition points at, for the repo at `slug`.

    `is_rendered` says whether this one reference is used as an image, which makes it a shield whatever host
    serves it. It is per reference rather than per URL deliberately: two names can point at one URL, and
    keying on the URL would make a plain link a shield because some other reference to the same address is
    rendered, which the spec's wording ("a badge, judged by the document rendering the reference as an image")
    does not say.

    Keying on the host instead read a badge from any other host as a plain URI and asked for it to be renamed
    `-link`, which is a rename away from the convention. The case that found it was a retired last-build
    service, which deprecatedShields now reports separately, but the rule is about how a reference is used
    rather than about any one host, so there is no img.shields.io short-circuit here: across the fleet all 119
    img.shields.io definitions are rendered as images, so a host test classifies nothing usage does not and
    can only contradict the rule it sits beside.

    A reference is local only when it carries no URI scheme, meaning a path inside this repo. A `mailto:`,
    `ssh:` or `ftp:` target is a link rather than a file, and reading it as local would demand a bare name and
    the Repo group for it. No fleet README carries one today, so this is a shape the rule has to get right
    before one does rather than a defect being repaired.

    Distribution is scoped to this repo's own URLs, so a link to somebody else's GitHub repo or Docker Hub
    image, which a 3rd Party Tools list is full of, stays external rather than being read as a channel of this
    project's. Those prefixes come from the model rather than from a second list here, since ownership stated
    in two places is two things that can disagree, and a canonicalLinks entry is consulted only for a
    reference this already classified distribution.
    """
    if url.startswith("#"):
        return "anchor"
    if not url.startswith(("http://", "https://")) and not _URI_SCHEME.match(url):
        return "local"
    if prefixes and url.lower().startswith(tuple(prefixes)):
        return "distribution"
    return "shield" if is_rendered else "external"


def canonical_link_entry(url, model, slug):
    """The canonicalLinks entry `url` is a destination for, or None where the model fixes no name for it.

    A destination every repo has is called the same thing in every repo, so a reader moving between them is
    not re-learning names.
    """
    base = f"https://github.com/{slug}"
    bare = url.rstrip("/")
    for c in model.get("canonicalLinks", []):
        if "match" in c:
            if re.search(c["match"], url):
                return c
            continue
        if "repoPath" not in c:
            # Loud rather than skipped, since skipping would quietly stop enforcing this destination's name.
            # The schema requires one of the two and validate.py gates it, so reaching here means a model nothing checked.
            raise KeyError(
                f"spec/readme-sections.json: canonicalLinks entry '{c.get('name')}' carries neither repoPath nor match"
            )
        want = (base + c["repoPath"]).rstrip("/")
        if (
            bare.lower().startswith(want.lower())
            if c.get("prefix")
            else bare.lower() == want.lower()
        ):
            return c
    return None


def canonical_name_findings(defs, model, slug, rendered=(), prefixes=()):
    """Reference names against canonicalLinks, resolved over the whole definition set rather than one at a time.

    A perTarget destination is one a repo may publish several of, and its rule is a count: the bare canonical
    name where there is exactly one, and `<target>-<name>` where there are several. Judging a definition alone
    cannot see which case it is in, which is why this is a second pass over the collected set. The qualifier is
    a prefix on the same name rather than a different name, so one shape covers PlexCleaner's single image and
    NxWitness's twelve.

    Only a distribution-kind link is renamed, so an upstream image a repo happens to link is left alone.
    ESPHome-Config links `hub.docker.com/r/esphome/esphome`, which is upstream's image and not a channel of
    its own, and matching the host alone told it to call that `docker-hub-link`.
    """
    findings, hits = [], {}
    for ref, url in defs:
        if link_kind(url, slug, ref in rendered, prefixes) != "distribution":
            continue
        c = canonical_link_entry(url, model, slug)
        if c:
            hits.setdefault(c["name"], (c, []))[1].append((ref, url))
    for name, (c, group) in hits.items():
        if c.get("perTarget") and len(group) > 1:
            for ref, url in group:
                if not (ref.endswith(f"-{name}") and len(ref) > len(name) + 1):
                    findings.append(
                        (
                            "LETTER",
                            f"readme: the reference `[{ref}]` points at {url} - this repo publishes {len(group)} of these, so each is named `<target>-{name}` (spec/readme-structure.md)",
                        )
                    )
        else:
            for ref, url in group:
                if ref != name:
                    findings.append(
                        (
                            "LETTER",
                            f"readme: the reference `[{ref}]` points at {url} and is named `{name}` in every repo - a shared destination carries a shared name (spec/readme-structure.md)",
                        )
                    )
    return findings


def readme_link_findings(text, model, slug):
    """Reference-definition naming and grouping, per the linkGroups, linkNaming and canonicalLinks model.

    The naming half is a LETTER because the fleet already meets it: 119 of 122 shield references end
    `-shield` and 514 of 532 URI references end `-link`, so the rule is written down rather than imposed.
    The grouping half is a DRIFT because it is not met: the fleet carries seventeen distinct group-header
    names across twenty-two repos, and gating that would bury the naming findings under a re-grouping sweep
    of every README at once.
    """
    findings = []
    suffixes = {n["kind"]: n["suffix"] for n in model["linkNaming"]}
    prefixes = distribution_prefixes(model, slug)
    groups = model["linkGroups"]
    declared = [g["name"] for g in groups]
    holds = {g["name"].lower(): g["holds"] for g in groups}

    # Walk the definitions in order, tracking which group header each one falls under.
    # A comment counts as a group header only where a definition actually falls under it, since the closed set governs the reference-definition block and not every comment in the file.
    # A tool directive is skipped outright rather than relying on that, because one placed inside the reference block does have definitions under it and would be reported as an unknown group.
    # The prefixes are declared in the model rather than guessed from the shape of the text, since which tools a repo instructs is a fact about the fleet rather than something to infer.
    unfenced = unfenced_text(text)
    seen_headers, current, in_group = [], None, {}
    for ln in unfenced.split("\n"):
        s = ln.strip()
        h = re.fullmatch(r"<!--\s*(.*?)\s*-->", s)
        if h and "omit from toc" not in h.group(1):
            if not any(
                h.group(1).lower().startswith(d.lower()) for d in model.get("directiveComments", [])
            ):
                current = h.group(1)
            continue
        d = _LINK_DEF.match(s)
        if d:
            if current is not None and current not in in_group:
                seen_headers.append(current)
            in_group.setdefault(current, []).append((d.group(1), d.group(2)))

    all_defs = [p for v in in_group.values() for p in v]
    # A reference the document renders as an image is a shield whatever host serves it, so resolve those first.
    # Read over the unfenced text for the same reason the definitions are.
    # An `![alt][ref]` inside a code sample is markup being shown rather than a badge being rendered, and counting it would make a plain link a shield.
    by_ref = dict(all_defs)
    rendered = {m.group(1) for m in _MD_IMAGE_REF.finditer(unfenced) if m.group(1) in by_ref}
    described = {"shield": "a badge", "anchor": "an in-page anchor", "local": "a path in this repo"}
    for ref, url in all_defs:
        kind = link_kind(url, slug, ref in rendered, prefixes)
        want = suffixes.get(kind, "")
        got = "-shield" if ref.endswith("-shield") else "-link" if ref.endswith("-link") else ""
        if got != want:
            shown = f"`{want}`" if want else "a bare name with no suffix"
            findings.append(
                (
                    "LETTER",
                    f"readme: the reference `[{ref}]` points at {described.get(kind, 'a URI')} and should end in {shown} (spec/readme-structure.md)",
                )
            )
    findings += canonical_name_findings(all_defs, model, slug, rendered, prefixes)

    unknown = [h for h in seen_headers if h not in declared]
    if unknown:
        findings.append(
            (
                "DRIFT",
                f"readme: link-group header(s) outside the declared set: {', '.join(sorted(set(unknown)))} - the groups are {', '.join(declared)} (spec/readme-structure.md)",
            )
        )
    ordered = [h for h in seen_headers if h in declared]
    if ordered != sorted(ordered, key=declared.index):
        findings.append(
            (
                "DRIFT",
                f"readme: the link groups run {', '.join(ordered)} rather than the declared order (spec/readme-structure.md)",
            )
        )
    if in_group.get(None):
        # Distinguish no grouping at all from a stray definition above the first header.
        # Two repos carry the whole block ungrouped, and "116 definitions sit above the first header" describes that badly.
        if not seen_headers:
            findings.append(
                (
                    "DRIFT",
                    f"readme: the {len(in_group[None])} reference definitions carry no group headers - they are grouped under {', '.join(declared)} (spec/readme-structure.md)",
                )
            )
        else:
            findings.append(
                (
                    "DRIFT",
                    f"readme: {len(in_group[None])} reference definition(s) sit above the first group header (spec/readme-structure.md)",
                )
            )
    for header, defs in in_group.items():
        if header is None or not defs:
            continue
        names = [r for r, _ in defs]
        if names != sorted(names):
            findings.append(
                (
                    "DRIFT",
                    f"readme: the `{header}` group is not sorted by reference name (spec/readme-structure.md)",
                )
            )
        want_kind = holds.get(header.lower())
        strays = (
            {link_kind(u, slug, r in rendered, prefixes) for r, u in defs} - {want_kind}
            if want_kind
            else set()
        )
        if strays:
            findings.append(
                (
                    "DRIFT",
                    f"readme: the `{header}` group holds {', '.join(sorted(strays))} reference(s) where it holds {want_kind} (spec/readme-structure.md)",
                )
            )
    return findings


# A tool row is read as text for its reference name, with both outer pipes optional as GitHub's Markdown makes them.
_TOOL_ROW = re.compile(r"^\|?\s*\[([^\]]+)\]\[([^\]]+)\]\s*\|\s*([^|]*?)\s*(?:\||$)")
_TOOL_BULLET = re.compile(r"^[-*]\s*\[([^\]]+)\]\[([^\]]+)\]\s*(.*)$")


def third_party_tool_findings(text, catalog):
    """A repo's 3rd Party Tools entries against the shared catalog in spec/third-party-tools.json.

    The catalog is a standard set rather than a complete one. A repo's tools are mostly its own, so a tool the
    catalog does not name produces nothing at all: what is checked is the intersection, meaning that a repo
    using a tool the fleet has standardized links it by the same URL and describes it the same way. Twelve
    tools already appear in more than one repo, and three of them are linked by two different URLs today, which
    is the divergence this closes.

    Matching is by the tool's display name, the text a README links, since that is what a reader compares
    across repos and what the catalog is keyed on.

    The License column is read off the table's header rather than off its rows, so it reports once for the
    table instead of once per tool, and it is the only extra column named. spec/readme-structure.md forbids
    that one column and no other, and a check that flagged every extra column would be grading the fleet on a
    rule nobody wrote.
    """
    body = readme_region(text, "3rd Party Tools")
    if body is None:
        return []  # the absent section is already one finding from readme_section_findings
    declared = {t["name"].lower(): t for t in catalog["tools"]}
    # Read over the unfenced text, as readme_link_findings does: a sample footnote is not a definition.
    defs = {m.group(1): m.group(2) for m in _LINK_DEF.finditer(unfenced_text(text))}
    findings = []
    listed = []
    in_head = False
    for tok in _MD.parse(body):
        if tok.type in ("thead_open", "thead_close"):
            in_head = tok.type == "thead_open"
        elif in_head and tok.type == "inline" and tok.content.strip().lower() == "license":
            findings.append(
                (
                    "LETTER",
                    "readme: 3rd Party Tools carries a License column - a license belongs to the dependency and is authoritative at its source, so restating it adds a maintenance obligation and no information (spec/readme-structure.md)",
                )
            )
    for ln in body.split("\n"):
        m = _TOOL_ROW.match(ln.strip()) or _TOOL_BULLET.match(ln.strip())
        if not m:
            continue
        name, ref, desc = m.group(1), m.group(2), m.group(3).strip(" -:")
        listed.append(name)
        want = declared.get(name.lower())
        if want is None:
            continue
        url = defs.get(ref)
        if url is not None and url.rstrip("/") != want["link"].rstrip("/"):
            findings.append(
                (
                    "LETTER",
                    f"readme: 3rd Party Tools links {name} as {url} where the fleet links it as {want['link']} - a shared tool carries one link (spec/third-party-tools.json)",
                )
            )
        if desc != want["description"]:
            shown = f"'{desc}'" if desc else "no description"
            findings.append(
                (
                    "LETTER",
                    f"readme: 3rd Party Tools describes {name} as {shown} where the fleet describes it as '{want['description']}' - a shared tool carries one description (spec/third-party-tools.json)",
                )
            )
    # Ordering covers every tool listed, not only the cataloged ones, since a reader scans the whole list.
    if listed != sorted(listed, key=str.lower):
        first = next(
            f"{a} before {b}" for a, b in itertools.pairwise(listed) if a.lower() > b.lower()
        )
        findings.append(
            (
                "LETTER",
                f"readme: 3rd Party Tools is not alphabetized ({first}) - the list is scanned rather than read (spec/readme-structure.md)",
            )
        )
    return findings


def readme_shield_findings(text, model, entry):
    """Shield presence, by the additive classes in spec/readme-sections.json.

    Each shield names the section it belongs in, so the license shield is an ordinary member of the base class
    that happens to sit in the closing License section rather than a second model beside this one. Where a
    named section is absent, the shield is skipped, because readme_section_findings already reports the
    section and a second finding would describe the same gap.

    A class is a floor, so an extra shield is never a finding. The classes deliberately assert the endpoint
    and not the channel: a repo publishing several images carries a version shield per image per channel
    (NxWitness carries forty, across six images and four channels that are not `develop`), so a
    latest-plus-develop pair is the single-image form the spec recommends rather than something every docker
    repo can be measured against.
    """
    findings = []
    # One unfenced view serves both passes, so the definitions and the rendered images cannot be read from different documents.
    unfenced = unfenced_text(text)
    defs = {m.group(1): m.group(2) for m in _LINK_DEF.finditer(unfenced)}
    # A retired badge service is scanned across the whole document rather than per section, since a dead badge is wrong wherever it sits.
    # It renders broken rather than absent, which a visitor reads as a failing build rather than as a stale badge.
    # Both forms are read, since reading definitions alone made an inline badge invisible rather than wrong, which is the reading shield_endpoints already takes for every other shield.
    # A definition is reported even where nothing renders it, because a retired service left in the reference block is removed with the badge rather than after it.
    # Which of the four it is decides the wording, since a definition nothing renders is not rendering anything and saying so sends the reader looking for a badge that is not on the page.
    # Attribution is by reference name and never by URL: the same endpoint rendered inline leaves this definition unused, so reading the URL alone would credit a render to a reference nothing uses.
    # A set rather than the list shield_endpoints returns, since every use here is membership or a difference, and the names say which of the two namespaces each holds.
    rendered_urls = set(shield_endpoints(unfenced, defs))
    used_refs = {m.group(1) for m in _MD_IMAGE_REF.finditer(unfenced)}
    for dep in model.get("deprecatedShields", []):
        defined_urls = set()
        for ref, url in sorted(defs.items()):
            if dep["match"] in url:
                defined_urls.add(url)
                if ref in used_refs:
                    verb = f"renders {dep['label']}"
                elif url in rendered_urls:
                    verb = f"defines {dep['label']} and it is rendered elsewhere"
                else:
                    verb = f"defines {dep['label']} and nothing renders it"
                findings.append(
                    (
                        "LETTER",
                        f"readme: `[{ref}]` {verb}, which is retired - {dep['reason']} (spec/readme-structure.md)",
                    )
                )
        for url in sorted({u for u in rendered_urls if dep["match"] in u} - defined_urls):
            findings.append(
                (
                    "LETTER",
                    f"readme: an inline image renders {dep['label']}, which is retired - {dep['reason']} (spec/readme-structure.md)",
                )
            )
    targets = {(p.get("target") if isinstance(p, dict) else p) for p in entry.get("publish", [])}
    secrets = set(entry.get("requiredSecrets", []))
    want = []
    for cls in model["shieldClasses"]:
        t = cls["trigger"]
        if (
            t["kind"] == "always"
            or (t["kind"] == "publish" and t.get("target") in targets)
            or (t["kind"] == "secret" and t.get("name") in secrets)
        ):
            want += cls["shields"]
    for sh in want:
        region = addressed_region(text, sh["in"])
        if region is None:
            continue
        if not any(shield_matches(u, sh) for u in shield_endpoints(region, defs)):
            queries = "".join(f" with `{q}`" for q in sh.get("requireQuery", []))
            if sh.get("forbidQuery") is not None:
                queries += f" without `{sh['forbidQuery']}`"
            findings.append(
                (
                    "LETTER",
                    f"readme: `{sh['in']}` carries no {sh['label']} shield ({sh['match']}{queries}), which this repo's deliverables require (spec/readme-structure.md)",
                )
            )
        if not sh.get("exclusive"):
            continue
        # Rendered elsewhere, which for the license shield is the whole point: it belongs at the bottom and nowhere else.
        # Compared over uses rather than over raw text, so the shield's own `[ref]: url` definition is not read as a second placement.
        outside = strip_sections(
            _HTML_COMMENT.sub("", unfenced_text(text)), [sh["in"].split(">")[0].strip()]
        )
        if any(shield_matches(u, sh) for u in shield_endpoints(outside, defs)):
            findings.append(
                (
                    "LETTER",
                    f"readme: the {sh['label']} shield is rendered outside `{sh['in']}` - it belongs there and nowhere else (spec/readme-structure.md)",
                )
            )
    return findings


def description_findings(doc_texts, entry, live, slug):
    """The README title/tagline, the GitHub About description, and the Docker Hub short description, checked as one mirror set.

    Per spec/readme-structure.md item 1 and GOVERNANCE.md "Repository Details", the H1 is the repo name. The tagline
    after it, the first line of the intro region, is a link-free plain sentence of at most 100 characters that
    carries verbatim to the GitHub About description, and on a docker repo to the Docker Hub short description. Any
    further paragraph is free prose no mirror reads, which is why only the first line is measured.

    registry/repos.json's optional `description` is the canonical value once a repo declares it (GOVERNANCE.md
    "Repository Details"). repo-config/configure.sh then writes the About panel from that same field. A repo that
    has not adopted it yet still has the README as its source of truth, so the checks below fall back to the
    tagline exactly as before.

    A declared field's mirrors (About, Docker Hub) are measured against it even where the README itself could
    not be read (missing, oversized, or fetched with no inline content) or carries no usable tagline, since the
    declared value is canonical on its own and does not need the README to establish it.
    """
    findings = []
    declared = None
    if "description" in entry:
        # Delegates to validate.py's own contract instead of re-checking a second, easily-incomplete copy of it
        # (an earlier version here missed the link and length rules, accepting either as canonical).
        shape_errors = validate.description_errors(slug, entry["description"])
        if shape_errors:
            findings += [
                (
                    "DEFECT",
                    f"{msg}. Treated as undeclared here, since it fails spec/validate.py's contract.",
                )
                for msg in shape_errors
            ]
        else:
            declared = entry["description"]
    readme_want = None
    if "README.md" in doc_texts:
        title, intro = title_and_intro(doc_texts["README.md"])
        intro_line = tagline(intro)
        refs = references(doc_texts["README.md"])
        # The H1 is the repository name, and a hyphenated name may render its hyphens as spaces.
        # Use the GitHub API's canonical name, since the registry-URL slug can carry a different case.
        repo_name = live.get("name") or slug.split("/")[-1]
        if not title:
            findings.append(
                (
                    "LETTER",
                    "readme: no `# ` H1 title - the README opens with `# <repo name>` then the tagline (spec/readme-structure.md)",
                )
            )
        elif title.replace("-", " ") != repo_name.replace("-", " "):
            findings.append(
                (
                    "LETTER",
                    f"readme: the H1 title '{title}' is not the repo name '{repo_name}' (a hyphenated name may render its hyphens as spaces) - the H1 is the repository name (spec/readme-structure.md)",
                )
            )
        if not intro_line:
            findings.append(
                (
                    "LETTER",
                    "readme: no tagline after the H1 - the README opens with the title then a one-line description, which doubles as the About description (spec/readme-structure.md)",
                )
            )
        else:
            if strip_md_links(intro_line, refs) != intro_line:
                findings.append(
                    (
                        "LETTER",
                        "readme: the tagline carries Markdown links - keep it link-free plain text, it doubles as the repo About description (spec/readme-structure.md)",
                    )
                )
            readme_want = strip_md_links(intro_line, refs).strip()
            if len(readme_want) > 100:
                findings.append(
                    (
                        "LETTER",
                        f"readme: the tagline is {len(readme_want)} characters, over the 100-char limit (Docker Hub's short-description cap, the tightest surface it feeds) - tighten it to one short sentence (spec/readme-structure.md)",
                    )
                )
    # The declared field wins once a repo has one; every mirror (README, About, Docker Hub) is then measured against it.
    # A repo with no declared field keeps the README as the source, exactly as before, so with no readable tagline
    # there is nothing yet to measure the other mirrors against.
    want = declared or readme_want
    if want is None:
        return findings
    source = "registry/repos.json" if declared else "the README"
    if declared and readme_want is not None and readme_want != declared:
        findings.append(
            (
                "LETTER",
                f"readme: the tagline ('{readme_want}') does not match the declared description ('{declared}') - registry/repos.json's description is canonical once declared, so the README follows it (GOVERNANCE.md Repository Details)",
            )
        )
    desc = (live.get("description") or "").strip()
    if desc != want:
        findings.append(
            (
                "LETTER",
                f"description: the About description does not match the {'declared description' if declared else 'README tagline'} (description '{desc}' vs '{want}') - set it from {source}, or sharpen {source} first if the description carries real detail (GOVERNANCE.md Repository Details)",
            )
        )
    # Docker Hub short description mirrors the same canonical value, for a repo that publishes a docker image.
    # A transient lookup failure surfaces as a DRIFT ("could not verify"), never aborting or silently passing.
    # A 404 (image not at the derived name) returns None and is skipped.
    if any(
        (pt.get("target") if isinstance(pt, dict) else pt) == "docker"
        for pt in entry.get("publish", [])
    ):
        try:
            dh = docker_hub_description(slug)
        except Exception as e:  # noqa: BLE001
            dh = None
            findings.append(
                (
                    "DRIFT",
                    f"description: could not read the Docker Hub short description to verify it mirrors {source} ({e}) - verify by hand",
                )
            )
        if dh is not None and dh.strip() != want:
            findings.append(
                (
                    "LETTER",
                    f"description: the Docker Hub short description ('{dh.strip()}') does not match '{want}' - set it from {source} (spec/readme-structure.md)",
                )
            )
    return findings


def workspace_cspell_words(text):
    """True if workspace/settings JSON carries its own cSpell word list - the block cspell.json canonicalizes.

    Matches the quoted setting keys case-insensitively, so a mere mention of the cspell.json file is not a hit.
    """
    low = text.lower()
    return any(
        key in low for key in ('"cspell.words"', '"cspell.userwords"', '"cspell.ignorewords"')
    )


def _compose(text):
    """The YAML node tree of `text`, or None where it does not parse."""
    try:
        return yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None


def _mapping(node):
    """A mapping node's children as `{key: node}`, `{}` for any other node."""
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {str(k.value): v for k, v in node.value if isinstance(k, yaml.ScalarNode)}


def _plain(node):
    """The Python value a node constructs, as the safe loader reads it."""
    return yaml.SafeLoader("").construct_object(node, deep=True)


def job_nodes(text):
    """The workflow's jobs as `{key: node}`, `{}` where it has none or does not parse."""
    return _mapping(_mapping(_compose(text)).get("jobs"))


def code_view(node, depth=0):
    """A node's keys and scalar values as lines indented two spaces per level, the shape a contract token is read in.

    A job's children sit at `depth` 2, its place in a workflow. A block-scalar body is a script or prose rather
    than structure, so it is left out, except an `if:` condition, folded onto one line.
    """
    out = []

    def walk(child, key, level):
        label = "  " * level + ("" if key is None else f"{key}:")
        if isinstance(child, yaml.MappingNode):
            out.extend([label] if key else [])
            for k, v in child.value:
                walk(v, k.value, level + 1)
        elif isinstance(child, yaml.SequenceNode):
            out.extend([label] if key else [])
            for v in child.value:
                walk(v, None, level + 1)
        elif key == "if":
            out.append(f"{label} {' '.join(str(child.value).split())}")
        elif child.style in ("|", ">"):
            out.extend([label] if key else [])
        else:
            out.append(f"{label} {child.value}".rstrip())

    if node is not None:
        walk(node, None, depth - 1)
    return "\n".join(out)


def split_jobs(text):
    """Each job's own source lines, `{key: text}`, cut at the parser's marks for the verbatim job hash.

    A comment dedented below the job keys belongs to no job, so it is left out of every region.
    """
    jobs = _mapping(_compose(text)).get("jobs")
    if not isinstance(jobs, yaml.MappingNode) or jobs.flow_style or not jobs.value:
        return {}
    lines = text.splitlines(keepends=True)
    indent = jobs.value[0][0].start_mark.column
    end = jobs.end_mark.line + (1 if jobs.end_mark.column else 0)
    starts = [k.start_mark.line for k, _ in jobs.value] + [end]
    return {
        str(k.value): "".join(
            ln
            for ln in lines[starts[i] : starts[i + 1]]
            if not (ln.lstrip().startswith("#") and len(ln) - len(ln.lstrip()) < indent)
        )
        for i, (k, _) in enumerate(jobs.value)
    }


def check_interface(path, contract, text):
    """DRIFT where a workflow breaks its fixed contract by name and wiring, never its body (spec/fidelity-model.md)."""
    try:
        yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return [("DRIFT", f"interface: {path} does not parse as YAML, so its contract is unread")]
    findings = []
    jobs = job_nodes(text)
    for k in contract.get("requiredJobKeys", []):
        if k not in jobs:
            findings.append(("DRIFT", f"interface: {path} missing required job '{k}'"))
    name = contract.get("requiredCheckName")
    nodes = (_mapping(job).get("name") for job in jobs.values())
    names = {n.value for n in nodes if isinstance(n, yaml.ScalarNode)}
    if name and name not in names:
        findings.append(
            (
                "DRIFT",
                f"interface: {path} missing the ruleset-bound check name '{name}' as a job name",
            )
        )
    tok = contract.get("artifactNameToken")
    if tok and tok not in code_view(_compose(text)):
        findings.append(
            ("DRIFT", f"interface: {path} missing the '{tok}<branch>-<target>' artifact handoff")
        )
    # An absent job is already a requiredJobKeys finding, so its tokens are not reported again.
    for job, toks in contract.get("requireTokensInJob", {}).items():
        if job in jobs:
            view = code_view(jobs[job], 2)
            for t in toks:
                if t not in view:
                    findings.append(
                        ("DRIFT", f"interface: {path} job '{job}' missing required '{t}'")
                    )
    for job, toks in contract.get("forbidTokensInJob", {}).items():
        if job in jobs:
            view = code_view(jobs[job], 2)
            for t in toks:
                if t in view:
                    findings.append(
                        ("DRIFT", f"interface: {path} job '{job}' uses forbidden '{t}'")
                    )
    return findings


# The callers whose validate job may declare validate-task.yml's `python-directories` input, per WORKFLOW.md.
PYTHON_DIRECTORIES_CALLERS = (
    ".github/workflows/test-pull-request.yml",
    ".github/workflows/publish-release.yml",
)


def normalize_declared_python_directory(line):
    """Mirror python_directories.py's normalize() for a valid line, None for one the hub validator refuses."""
    path = line.strip()
    if not path or "\t" in path or path.startswith("/") or "\\" in path or ".." in path.split("/"):
        return None
    return posixpath.normpath(path)


def python_directories_caller_findings(path, text, entry):
    """DRIFT where a caller's declared `python-directories` input disagrees with the registry's own.

    Compared as normalized sets of declarations, since naming `.` is not the same as naming nothing.
    Only the job keyed `validate` is compared, and validate-task.yml called from another job is reported.
    """
    if path not in PYTHON_DIRECTORIES_CALLERS:
        return []
    jobs = job_nodes(text)
    others = sorted(
        k for k, job in jobs.items() if k != "validate" and "validate-task.yml" in code_view(job)
    )
    unread = []
    if others:
        unread = [
            (
                "DRIFT",
                (
                    f"python-directories: {path} calls validate-task.yml from "
                    f"{'job' if len(others) == 1 else 'jobs'} {', '.join(others)}, and only the job "
                    "keyed 'validate' has its python-directories input compared against the registry."
                ),
            )
        ]
    validate_job = jobs.get("validate")
    if validate_job is None or "validate-task.yml" not in code_view(validate_job):
        return unread
    node = _mapping(_mapping(validate_job).get("with")).get("python-directories")
    declared_text = "" if node is None else _scalar_text(_plain(node)) or ""
    declared = sorted(
        {d for d in map(normalize_declared_python_directory, declared_text.splitlines()) if d}
    )
    registry = sorted(python_directories_of(entry))
    if declared == registry:
        return unread
    return unread + [
        (
            "DRIFT",
            (
                f"python-directories: {path} validate job passes {declared or 'none'}, registry "
                f"declares {registry or 'none'}"
            ),
        )
    ]


CONCURRENCY_CALLERS = {
    ".github/workflows/publish-release.yml": "${{ github.workflow }}-${{ github.ref }}",
    ".github/workflows/merge-bot-pull-request.yml": (
        "${{ github.workflow }}-${{ github.event.pull_request.number }}"
    ),
}
_RUN_ID_SUFFIX = re.compile(
    r"^\$\{\{github\.event_name=='push'&&\(github\.ref!='refs/heads/main'\|\|\((?:github\.actor!='ptr727-codegen\[bot\]'&&github\.actor!='dependabot\[bot\]'"
    r"|github\.actor!='dependabot\[bot\]'&&github\.actor!='ptr727-codegen\[bot\]')\)\)"
    r"&&format\('-\{0\}',github\.run_id\)\|\|''\}\}$"
)
_SCHEDULE_RUN_ID_SUFFIX = "${{github.event_name=='schedule'&&format('-{0}',github.run_id)||''}}"
_DISPATCH_TEST = "github.event_name=='workflow_dispatch'"
_EXPRESSION_SPACE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _strip_unquoted_space(value):
    """Whitespace removed except inside single-quoted strings, so `'a b'` stays distinct from `'ab'`."""
    parts = value.split("'")
    return "'".join(
        re.sub(r"\s+", "", part) if i % 2 == 0 else part for i, part in enumerate(parts)
    )


def _tight(value):
    return _EXPRESSION_SPACE.sub(lambda m: "${{" + m.group(1) + "}}", value)


def _scalar_text(value):
    """A YAML scalar as the string GitHub reads, or None for a mapping or a sequence."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return None
    return "" if value is None else str(value)


def _schedule_never_publishes(doc):
    """True where `on:` admits a `schedule` and no `push`, and every build-release-task.yml job requires a dispatch.

    The dispatch test must be a top-level conjunct of the job's `if:`, in a condition holding no `||` or negated group.
    """
    # PyYAML reads a bare `on` key as the boolean true, per YAML 1.1.
    on = doc.get("on", doc.get(True))
    triggers = set(on) if isinstance(on, (dict, list)) else {on}
    if "schedule" not in triggers or "push" in triggers:
        return False
    release_jobs = 0
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict) or "build-release-task.yml" not in str(job.get("uses", "")):
            continue
        release_jobs += 1
        tight = _strip_unquoted_space(_scalar_text(job.get("if")) or "")
        if tight.startswith("${{") and tight.endswith("}}"):
            tight = tight[3:-2]
        conjuncts = {term.strip("()") for term in tight.split("&&")}
        if "||" in tight or "!(" in tight or _DISPATCH_TEST not in conjuncts:
            return False
    return release_jobs > 0


def concurrency_caller_findings(path, text):
    """DRIFT where the publisher or the merge-bot declares a concurrency group other than the one WORKFLOW.md pins.

    The publisher's group is the ref-scoped base plus at most the push-only run-id suffix, or the schedule-only
    suffix where its schedule never publishes. The merge-bot's is the base keyed on the PR number. Either leaves
    `cancel-in-progress` false or unset.
    """
    base = CONCURRENCY_CALLERS.get(path)
    if base is None:
        return []
    unreadable = [
        (
            "DRIFT",
            f"concurrency: {path} writes its concurrency in a shape the audit cannot read. Write a block mapping with group and cancel-in-progress, each a single-line scalar.",
        )
    ]
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return unreadable
    if not isinstance(doc, dict):
        return unreadable
    concurrency = doc.get("concurrency")
    if concurrency is None:
        return [
            (
                "DRIFT",
                f"concurrency: {path} declares no workflow-level concurrency, expected group '{base}'",
            )
        ]
    raw_cancel = concurrency.get("cancel-in-progress") if isinstance(concurrency, dict) else None
    group = _scalar_text(concurrency.get("group") if isinstance(concurrency, dict) else concurrency)
    cancel = None if raw_cancel is None else _scalar_text(raw_cancel)
    if group is None or (raw_cancel is not None and cancel is None):
        return unreadable
    findings = []
    if not group:
        findings.append(("DRIFT", f"concurrency: {path} reads no group value, expected '{base}'"))
    else:
        tight_group, tight_base = _tight(group), _tight(base)
        suffix = tight_group[len(tight_base) :] if tight_group.startswith(tight_base) else None
        tight_suffix = _strip_unquoted_space(suffix) if suffix else ""
        publisher = path.endswith("publish-release.yml")
        schedule_suffix = publisher and tight_suffix == _SCHEDULE_RUN_ID_SUFFIX
        suffix_ok = suffix is not None and (
            suffix == ""
            or (publisher and bool(_RUN_ID_SUFFIX.match(tight_suffix)))
            or (schedule_suffix and _schedule_never_publishes(doc))
        )
        if not suffix_ok and schedule_suffix:
            findings.append(
                (
                    "DRIFT",
                    f"concurrency: {path} gives a schedule its own group, but the audit cannot read its schedule as never publishing. That needs an on: with no push trigger, and github.event_name == 'workflow_dispatch' as a top-level conjunct of each build-release-task.yml job's if:, an if: holding no || or negated group.",
                )
            )
        elif not suffix_ok:
            findings.append(("DRIFT", f"concurrency: {path} group is '{group}', expected '{base}'"))
    if cancel is not None and _tight(cancel).lower() not in ("false", "${{false}}"):
        findings.append(
            ("DRIFT", f"concurrency: {path} sets cancel-in-progress to '{cancel}', expected false")
        )
    return findings


def dependabot_ecosystems(text):
    """The package-ecosystem values a dependabot.yml declares, empty where it does not parse."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return set()
    updates = doc.get("updates") if isinstance(doc, dict) else None
    if not isinstance(updates, list):
        return set()
    return {
        str(u["package-ecosystem"])
        for u in updates
        if isinstance(u, dict) and u.get("package-ecosystem")
    }


@functools.lru_cache(
    maxsize=1024
)  # bounded; the keys that recur across repos are the canonical and its history
def _hash_normalized(norm_text):
    return hashlib.sha256(norm_text.encode("utf-8")).hexdigest()


def content_hash(text):
    # Cache on the normalized form, not raw text, so EOL-only variants (CRLF vs LF) share one entry.
    return _hash_normalized(normalize(text))


def classify_verbatim(down_text, canon_text, past_texts):
    """None if the downstream copy matches the current canonical, 'stale' if it matches a past hub revision
    (the base advanced - re-vendor), or 'modified' if it matches no revision the base ever produced (the
    repo changed fixed content). The discriminator is a content hash, never a version stamp - a stamp can
    claim to be current while the body was edited, so it is never trusted for integrity.
    """
    dh = content_hash(down_text)
    if dh == content_hash(canon_text):
        return None
    for past in past_texts:
        if content_hash(past) == dh:
            return "stale"
    return "modified"


class ShallowCloneError(RuntimeError):
    """The hub checkout is a shallow clone, a hub-local condition rather than a repo finding."""


@functools.cache
def _require_full_history(root):
    """Raise when `root` is a shallow clone, since every history read there is silently wrong.

    The probe is the git-path `shallow` file, which every git version writes for a shallow clone.
    Git before 2.15 echoes an unknown `--is-shallow-repository` flag, so that flag proves nothing.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--git-path", "shallow"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=root,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return
    if (pathlib.Path(root) / probe.stdout.strip()).exists():
        raise ShallowCloneError(
            f"the hub checkout at {root} is a shallow clone, so the stale-vs-modified "
            "classification and the intent staleness advisory would be wrong. "
            "Run `git fetch --unshallow origin` in that checkout."
        )


@functools.cache
def _hub_main_rev():
    """The hub's own `main`, fetched fresh from `origin` and resolved to a commit SHA.

    Reached as a checkout of one's own, fetched immediately before use, per AGENTS.md, rather
    than trusting ROOT's checked-out branch. `git fetch`
    writes into ROOT's own object database, so a separate clone is not needed. Resolved to a
    concrete SHA right away, not the mutable `FETCH_HEAD` pointer, so a later fetch elsewhere in
    the process cannot move it mid-run.

    Cached: one fetch and resolve per run, reused for every rel_path read.
    """
    fetch = subprocess.run(
        ["git", "fetch", "-q", "origin", "main"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch origin main failed in {ROOT}: {fetch.stderr.strip()}")
    resolved = subprocess.run(
        ["git", "rev-parse", "FETCH_HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise RuntimeError(f"git rev-parse FETCH_HEAD failed in {ROOT}: {resolved.stderr.strip()}")
    return resolved.stdout.strip()


@functools.cache
def _git_revisions(rel_path, rev=None):
    """Every commit that touched rel_path in the hub's history, newest first, as (date, sha, text).

    `text` is None where rel_path has no file content at this revision: absent (deleted, checked
    against the revision's own tree rather than `git show`'s stderr wording, which reads
    differently once rel_path exists again in a later commit), or present as something other than
    a regular file, by ls-tree mode rather than type (a directory from a file-to-directory
    transition, a symlink, whose ls-tree type is "blob" too but whose content is its target path
    rather than a file's, a submodule gitlink). A `git show` failure for any other reason (a
    permission or encoding fluke, a corrupt object) raises instead of folding into the same None,
    so a real command fault cannot pass as an ordinary absence. Cached because one canonical's
    history is read once per fidelity/staleness check, then reused for every audited repo's copy.

    `rev` names the git revision walked. It defaults to the hub's own `main` via
    `_hub_main_rev()` rather than the implicit HEAD of whatever
    branch ROOT has checked out. The --selftest fixtures pass an explicit `rev` to exercise a
    plain local branch in a throwaway repo with no `origin` to fetch, keeping the offline engine
    self-test offline.
    """
    _require_full_history(ROOT)
    walk_rev = _hub_main_rev() if rev is None else rev
    r = subprocess.run(
        ["git", "log", "--format=%cI %H", walk_rev, "--", rel_path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if r.returncode != 0:
        # A real command failure (not a git repo, a corrupt object) must not read as "no
        # history": that silently clears the intent-staleness advisory and drops verbatim's
        # past-revision list, both misreporting a tool fault as a clean audit.
        raise RuntimeError(f"git log failed for {rel_path}: {r.stderr.strip()}")
    if not r.stdout.strip():
        return []
    out = []
    for line in r.stdout.splitlines():
        date, sha = line.split(" ", 1)
        t = subprocess.run(
            ["git", "ls-tree", sha, "--", rel_path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
        if t.returncode != 0:
            # A real lookup failure (a corrupt object, not this sha): git ls-tree exits non-zero
            # only for that, never for a merely absent path (empty stdout, exit 0, below).
            raise RuntimeError(f"git ls-tree failed for {sha}:{rel_path}: {t.stderr.strip()}")
        entry = t.stdout.strip()
        mode = entry.split(None, 1)[0] if entry else None
        if mode not in ("100644", "100755"):
            # Absent (empty stdout), or present as something other than a regular file (a
            # directory from a file-to-directory transition, a symlink, a submodule gitlink):
            # none has file content to compare, so all read the same as a confirmed deletion. A
            # symlink's ls-tree type is "blob" too (its content is the link target), so the mode
            # is checked directly rather than the type.
            out.append((date, sha, None))
            continue
        # Decode as UTF-8 with replacement to match the downstream and canonical reads.
        # A divergent decode would fabricate a mismatch.
        s = subprocess.run(
            ["git", "show", f"{sha}:{rel_path}"],
            cwd=ROOT,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if s.returncode != 0:
            raise RuntimeError(f"git show failed for {sha}:{rel_path}: {s.stderr.strip()}")
        out.append((date, sha, s.stdout))
    return out


def git_file_history(rel_path):
    """Every past revision's content of a hub-tracked file (to tell a stale copy from a modified one)."""
    return [text for _, _, text in _git_revisions(rel_path) if text is not None]


def canonical_current_text(rel_path):
    """The hub's current canonical content of rel_path, from the same `_git_revisions()` call
    `git_file_history()` and `hub_last_change()` already make, so "current" and "history" can
    never disagree about which commit they read: reading "current" from ROOT's working tree
    while history walked the resolved `main` SHA let a copy that matches today's main misclassify
    as stale against its own most recent history entry.
    None if rel_path has no history at `main`, or its newest revision has no file content there.
    """
    revisions = _git_revisions(rel_path)
    return revisions[0][2] if revisions else None


def _last_effective_change(revisions):
    """The (date, sha) of the newest revision in `revisions` (newest-first (date, sha, text)
    triples for one file) whose content differs from its predecessor after normalize(), or the
    oldest (creation) revision if every later one differs from its predecessor only by normalized
    churn (spec/fidelity-model.md "Normalization": EOL, a Dependabot action-pin bump, a pruned
    `needs:` list). None if `revisions` is empty.

    The creation revision always counts as effective: it has no predecessor, and normalize()
    applied to only one side proves nothing. A revision with unreadable text (None) also counts as
    effective rather than being silently skipped, since a real difference cannot be ruled out.

    `revisions` is assumed newest-first with each entry the immediate predecessor of the one
    before it (verified empirically over every intent-fidelity canonical file's full history):
    this repo's own branching is forward-only (no back-merges from
    main into develop) with feature branches squash-merged one at a time, so a canonical file's
    per-path `git log` is a single line, not a graph with siblings to mis-order. A canonical file
    reached through a genuinely branching history (a direct hotfix to main alongside independent
    develop work touching the same path) would need each revision compared against its actual git
    parent(s) instead of its list neighbor.
    """
    for i, (date, sha, text) in enumerate(revisions):
        if i + 1 == len(revisions):
            return date, sha
        older_text = revisions[i + 1][2]
        if text is None or older_text is None or normalize(text) != normalize(older_text):
            return date, sha
    return None


@functools.cache
def git_blob_in_file_history(rel_path, blob_sha, rev=None):
    """Whether a blob occurred in a path's hub history.

    `rev` defaults to the hub's own `main` via `_hub_main_rev()`
    for the same reason `_git_revisions` does: ROOT's checked-out branch is not necessarily
    `main`, and a develop-only revision matching `blob_sha` must not read as "stale" against a
    hub history `main` doesn't actually contain.
    """
    _require_full_history(ROOT)
    walk_rev = _hub_main_rev() if rev is None else rev
    result = subprocess.run(
        ["git", "log", "--format=%H", f"--find-object={blob_sha}", walk_rev, "--", rel_path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


@functools.cache
def hub_last_change(rel_path):
    """The hub checkout's most recent EFFECTIVE change to rel_path, as (iso_date, short_sha), or
    None if untracked.

    "Effective" excludes normalized churn (spec/fidelity-model.md "Normalization": EOL, a
    Dependabot action-pin bump, a pruned `needs:` list) the same way the verbatim check already
    does, via _last_effective_change over the file's full history. A raw last-commit date treated
    every one of those bumps as the copy "trailing", even though the fidelity model already
    classifies that class as governed per-repo drift rather than a deviation.

    Cached because one canonical's date is compared against every audited repo's copy.
    """
    change = _last_effective_change(_git_revisions(rel_path))
    if change is None:
        return None
    date, sha = change
    return date, sha[:7]


def intent_canonical_rel(item, path):
    """The hub path an intent unit's copy is judged against for staleness.

    `reference` wins where the manifest sets one.
    Otherwise the intent unit's own canonical, `intentRef`, wins.
    Otherwise the unit compares against its own `path`.
    Only `intentRef` ever carries a `#anchor`, routing a reader to one section of a larger doc.
    The anchor names a place to read, not a narrower file to diff against, so it is stripped there and nowhere else, comparing the whole canonical file instead.
    """
    # spec/validate.py shape-checks these fields, but this engine runs standalone and does not invoke it first.
    ref = item.get("reference")
    if isinstance(ref, str) and ref:
        return ref
    intent = item.get("intentRef")
    if isinstance(intent, str) and intent:
        canonical = intent.split("#", 1)[0]
        if canonical:
            return canonical
    return path


def declared_alternatives(baseline):
    """Map each baseline path to the `alternatives` its entry declares, the other paths it may sit at.

    spec/validate.py holds a path declaring alternatives to a single entry, so nothing here merges.
    """
    return {
        item["path"]: list(item["alternatives"]) for item in baseline if item.get("alternatives")
    }


def contents_text(content):
    """The decoded text of a contents API response, or None where it holds no inline file content.

    A directory returns a list, and a too-large file returns encoding "none". An empty file decodes
    to an empty string, so the guard is on the encoding rather than on truthiness.
    """
    if not isinstance(content, dict) or content.get("encoding") != "base64":
        return None
    return base64.b64decode(content["content"]).decode("utf-8", "replace")


def read_baseline_file(slug, ground, path, alternatives=()):
    """The path a baseline file was found at and its contents response, or `(path, None)`.

    The declared path is read first and an alternative only while nothing is found. A repo holding
    both is therefore read at the declared path.
    """
    for candidate in (path, *alternatives):
        content = gh(f"repos/{slug}/contents/{candidate}?ref={ground}", ok404=True)
        if content is not None:
            return candidate, content
    return path, None


def baseline_file_findings(slug, ground, path, alternatives, item, is_hub):
    """Read one baseline file, returning `(content, text, findings)` for what reading it alone owes.

    Where no path is present, an interface unit's absence is DRIFT, since a workflow's naming varies
    more than a carried config's. Any other unit's absence is a file-presence LETTER. `text` is None
    where the contents API returned no inline content, as for a too-large file, while an empty file
    decodes to an empty string. An intent unit outside the hub gets its staleness advisory, read at
    the path the file was found at. The hub's own copies are the canonicals, so it has none.
    """
    found, content = read_baseline_file(slug, ground, path, alternatives)
    fid = item.get("fidelity") if item else "presence"
    if content is None:
        if fid == "interface":
            return (
                None,
                None,
                [("DRIFT", f"interface: {path} absent on {ground}, cannot verify its contract")],
            )
        named = " or ".join((path, *alternatives))
        return (
            None,
            None,
            [
                (
                    "LETTER",
                    f"file: {named} absent on {ground} (verify intent per AUDIT.md section 7)",
                )
            ],
        )
    text = contents_text(content)
    if item is None or fid != "intent" or is_hub:
        return content, text, []
    return (
        content,
        text,
        check_intent_staleness(slug, ground, found, intent_canonical_rel(item, path), text),
    )


def check_intent_staleness(slug, ground, path, canonical_rel, down_text):
    """The intent-staleness advisory: a last-modified comparison, since intent has no content check.

    An intent unit is judged by meaning, so the audit asserts presence and nothing about content
    (spec/fidelity-model.md), which is how a copy trailed the hub by many revisions while every
    check read clean. No reconciliation record exists anywhere, so the implementable proxy is
    when each side last changed: the hub canonical changing after the repo's copy marks the copy
    as possibly trailing. "Changed" excludes normalized churn (hub_last_change), so a Dependabot
    action-pin bump or a needs-list prune does not by itself mark every carrier as trailing.
    Advisory only, DRIFT and never a failure, and honest about its blind spot: a copy touched
    after the hub change without actually reconciling reads current.

    A copy content-identical to the canonical cannot trail it, so that case is skipped however
    old the copy's last commit is. It is also the promotion candidate spec/fidelity_honesty.py
    exists to find, where the structural fix is verbatim fidelity rather than a better advisory.
    """
    hub_change = hub_last_change(canonical_rel)
    if hub_change is None:
        return []
    if down_text is not None:
        canon_text = canonical_current_text(canonical_rel)
        if canon_text is not None and content_hash(down_text) == content_hash(canon_text):
            return []
    commits = gh(f"repos/{slug}/commits?path={path}&sha={ground}&per_page=1")
    if not commits:
        return []
    repo_date = commits[0]["commit"]["committer"]["date"]
    hub_date, hub_sha = hub_change
    if datetime.fromisoformat(hub_date) <= datetime.fromisoformat(repo_date):
        return []
    return [
        (
            "DRIFT",
            (
                f"intent: {path} last changed {repo_date} on {ground}, and the hub canonical "
                f"changed later at {hub_date} ({hub_sha}) - the copy possibly trails the hub, "
                f"verify intent per AUDIT.md section 7"
            ),
        )
    ]


def check_verbatim(label, down_text, canonical_rel, extract=None):
    """Compare a downstream copy against the hub's canonical (a region if `extract` is given), EOL-normalized,
    and classify a mismatch as stale or modified via the canonical's git history. All findings are DRIFT: a
    byte diff is a hint to review, never proof of breakage.
    """
    # canonical_current_text() and git_file_history() both read the same _git_revisions() call, so a copy matching today's main can never mismatch against its own history's newest entry.
    canon_text = canonical_current_text(canonical_rel)
    if canon_text is None:
        return [
            (
                "DRIFT",
                f"verbatim: {label} canonical {canonical_rel} is unreadable from the hub (spec error?)",
            )
        ]
    history = git_file_history(canonical_rel)
    if extract is not None:
        down_region, canon_region = extract(down_text), extract(canon_text)
        if canon_region is None:
            return [("DRIFT", f"verbatim: {label} region absent in the canonical (spec error?)")]
        if down_region is None:
            return [("DRIFT", f"verbatim: {label} region absent downstream, cannot compare")]
        down_text, canon_text = down_region, canon_region
        history = [h for h in (extract(t) for t in history) if h is not None]
    verdict = classify_verbatim(down_text, canon_text, history)
    if verdict is None:
        return []
    if verdict == "stale":
        return [
            (
                "DRIFT",
                f"verbatim: {label} matches a past hub revision, not the current canonical - the base advanced, re-vendor it",
            )
        ]
    return [
        (
            "DRIFT",
            f"verbatim: {label} differs from the canonical and matches no past hub revision - the repo modified fixed content, review it",
        )
    ]


def classify_branch_drift(base, main, develop):
    """Split the paths main changed since the merge-base by direction, given {path: object-sha} maps
    for the merge-base, main-head, and develop-head trees. `behind`: main moved the path and develop
    still holds the merge-base object - a genuine forward-sync gap. `diverged`: both branches moved
    the path, so develop may already supersede main and a blind sync from main would revert it. A
    path only develop moved is not main-side drift and is excluded. Returns (behind, diverged)."""
    changed_on_main = {p for p in set(base) | set(main) if base.get(p) != main.get(p)}
    differ = [p for p in changed_on_main if main.get(p) != develop.get(p)]
    behind = sorted(p for p in differ if develop.get(p) == base.get(p))
    diverged = sorted(p for p in differ if develop.get(p) != base.get(p))
    return behind, diverged


def ground_branch_of(entry, branch=None, defaults=None):
    """The branch this audit reads, the registry's `groundTruthBranch` unless overridden.

    Resolved in three steps, the entry, then the registry defaults, then the fleet value, which is the shape
    audit_repo() already uses for workflowModel. Every reader used to fall back to a literal "main" and consult no
    defaults at all, so a registry setting `defaults.groundTruthBranch` to develop validated clean and every repo
    relying on it was still audited against main, with nothing reporting that the declaration went unread.

    Presence is the test at both levels rather than truthiness, matching spec/validate.py, which holds the defaults
    key to the same grammar as the per-repo one: a declared empty string is a value that gate refuses by name, not
    a field to default away here.
    """
    if branch:
        return branch
    if "groundTruthBranch" in entry:
        return entry["groundTruthBranch"]
    defaults = defaults or {}
    if "groundTruthBranch" in defaults:
        return defaults["groundTruthBranch"]
    return "main"


@functools.cache
def registered_checks():
    """spec/checks.json's checks, in the order the audit runs them."""
    return tuple(load("spec/checks.json")["checks"])


def check_function(check):
    """The function a registered check names, `check_<id>` with each dot and hyphen as an underscore."""
    return globals()["check_" + re.sub(r"[.-]", "_", check["id"])]


@dataclass
class Run:
    """One repository's audit: what every registered check reads, and the findings so far."""

    entry: dict
    spec: dict
    slug: str
    ground: str
    live: dict
    main: Any
    develop: Any
    head: Any = None
    findings: list = field(default_factory=list)
    content: dict = field(default_factory=dict)

    @functools.cached_property
    def hub(self):
        return self.entry.get("name") == hub_repo_name()

    @functools.cached_property
    def sel(self):
        return repo_selectors(self.entry, self.spec["registry"].get("defaults", {}))

    @functools.cached_property
    def blobs(self):
        return repo_tree_blobs(self.slug, self.head)

    @functools.cached_property
    def entries(self):
        return blob_field(self.blobs, 0)

    @functools.cached_property
    def tree(self):
        return None if self.entries is None else set(self.entries)

    @functools.cached_property
    def coverage_types(self):
        candidates = coverage_candidate_types(
            declared_types(self.entry), self.entry.get("profiles", {})
        )
        mechanisms = self.spec["secrets"].get("typeMechanisms", {})
        return coverage_claiming_types(candidates, mechanisms, self.tree)

    @functools.cached_property
    def baseline(self):
        """`{path: (item, sections, verbatim sections)}` for each baseline file applying here, in manifest order.

        Sections union across same-path entries, and the last fidelity-bearing entry for a path is its item.
        """
        out = {}
        for item in self.spec["files"]["baseline"]:
            path = item["path"]
            if not applies(item.get("appliesTo", "*"), self.sel):
                continue
            # A coverage feature file is N/A where no type claims codecov (spec/type-model.md).
            if path == "codecov.yml" and not self.coverage_types:
                continue
            prior, needed, verbatim = out.get(path, (None, set(), set()))
            needed |= set(required_sections(item, self.sel))
            verbatim |= set(verbatim_sections(item, self.sel))
            fidelity = item.get("fidelity") in ("interface", "verbatim", "intent")
            out[path] = (item if fidelity else prior, needed, verbatim)
        return out

    @functools.cached_property
    def alternatives(self):
        return declared_alternatives(self.spec["files"]["baseline"])

    @property
    def docs(self):
        """README.md and HISTORY.md as read, for the checks that compare the two."""
        return {
            p: self.content[p]
            for p in ("README.md", "HISTORY.md")
            if self.content.get(p) is not None
        }

    def read_text(self, path):
        url = f"repos/{self.slug}/contents/{urllib.parse.quote(path)}?ref={self.ground}"
        return contents_text(gh(url))

    def unread(self, family, path, purpose):
        return (
            "DRIFT",
            f"{family}: could not read {path} content on {self.ground} to {purpose} (no inline content returned); verify by hand",
        )


def audit_repo(entry, spec, branch=None):
    """Run every registered check against one repository, returning `(findings, audited sha)`."""
    slug = repo_slug(entry)
    try:
        live = gh(f"repos/{slug}")
    except RuntimeError as e:
        return [("ERROR", str(e))], ""
    run = Run(
        entry,
        spec,
        slug,
        ground_branch_of(entry, branch, spec["registry"].get("defaults", {})),
        live,
        gh(f"repos/{slug}/branches/main", ok404=True),
        gh(f"repos/{slug}/branches/develop", ok404=True),
    )
    run.head = {"main": run.main, "develop": run.develop}.get(run.ground)
    if run.head is None:
        run.head = gh(f"repos/{slug}/branches/{run.ground}", ok404=True)
    checks = [c for c in registered_checks() if c["hub"] or not run.hub]
    if run.head is None:
        # Every content read would 404, so only what needs none runs.
        for check in checks:
            if not check.get("ground", True):
                run.findings += check_function(check)(run)
        missing = (
            f"branch: ground-truth branch {run.ground} does not exist, so nothing could be read"
        )
        return run.findings + [("ERROR", missing)], ""
    for i, check in enumerate(checks):
        if check["scope"] == "repo":
            run.findings += check_function(check)(run)
        elif i == 0 or checks[i - 1]["scope"] == "repo":
            # A run of per-file checks reads the baseline file by file, so findings group by file.
            stage = list(itertools.takewhile(lambda c: c["scope"] != "repo", checks[i:]))
            for path in run.baseline:
                for each in stage:
                    if each["scope"] == "baseline" or path in run.content:
                        run.findings += check_function(each)(run, path)
    return run.findings, run.head.get("commit", {}).get("sha", "")


def check_branch_facts(run):
    findings = [] if run.main is not None else [("DEFECT", "branch: main does not exist")]
    if bool(run.entry.get("hasDevelop")) != (run.develop is not None):
        state = "exists" if run.develop is not None else "is absent"
        findings.append(
            ("DRIFT", f"registry: hasDevelop={run.entry.get('hasDevelop')} but develop {state}")
        )
    return findings


def check_branch_drift(run):
    """Main-side changes develop lacks, split by direction, from the merge-base tree rather than the capped compare.

    Identical head trees are the fast path, since a merge-commit promotion leaves main permanently ahead.
    """
    if run.main is None or run.develop is None:
        return []
    if (
        run.main["commit"]["commit"]["tree"]["sha"]
        == run.develop["commit"]["commit"]["tree"]["sha"]
    ):
        return []
    cmp = gh(f"repos/{run.slug}/compare/develop...main", ok404=True)
    if not cmp or not cmp.get("files"):
        return []
    shas = {
        "base": cmp["merge_base_commit"]["commit"]["tree"]["sha"],
        "develop": run.develop["commit"]["commit"]["tree"]["sha"],
        "main": run.main["commit"]["commit"]["tree"]["sha"],
    }
    trees = {k: gh(f"repos/{run.slug}/git/trees/{sha}?recursive=1") for k, sha in shas.items()}
    if not all(isinstance(t, dict) for t in trees.values()) or any(
        t.get("truncated") for t in trees.values()
    ):
        return [
            (
                "DRIFT",
                f"branch: {len(cmp['files'])}+ path(s) differ between main and develop (forward-sync or reconciliation may be needed; tree unavailable or too large to classify direction)",
            )
        ]
    objs = {
        name: {e["path"]: e["sha"] for e in t["tree"] if e["type"] in ("blob", "commit")}
        for name, t in trees.items()
    }
    behind, diverged = classify_branch_drift(objs["base"], objs["main"], objs["develop"])
    findings = []
    if behind:
        shown = ", ".join(behind[:8]) + (" ..." if len(behind) > 8 else "")
        findings.append(
            (
                "DRIFT",
                f"branch: {len(behind)} main-side path change(s) develop is behind on (forward-sync needed): {shown}",
            )
        )
    if diverged:
        shown = ", ".join(diverged[:8]) + (" ..." if len(diverged) > 8 else "")
        findings.append(
            (
                "DRIFT",
                f"branch: {len(diverged)} path(s) changed on both main and develop since the merge-base - reconcile before promoting (develop may already supersede main): {shown}",
            )
        )
    return findings


def check_settings_general(run):
    expected = dict(run.spec["settings"])
    expected["has_discussions"] = not run.live.get("private", True)
    findings = [
        ("DEFECT", f"settings: {key} live={run.live.get(key)} expected={expected[key]}")
        for key in SETTINGS_KEYS + ["has_discussions"]
        if key in expected and run.live.get(key) != expected[key]
    ]
    if run.main is not None and run.live.get("default_branch") != "main":
        findings.append(
            (
                "DEFECT",
                f"settings: default_branch is {run.live.get('default_branch')}, expected main",
            )
        )
    return findings


def check_rulesets_payload(run):
    model = (
        run.entry.get("workflowModel")
        or run.spec["registry"].get("defaults", {}).get("workflowModel")
        or "release"
    )
    dev_payload = (
        "repo-config/operational/develop.json"
        if model == "operational"
        else "repo-config/develop.json"
    )
    expect = {"develop": load(dev_payload), "main": load("repo-config/main.json")}
    # A repo with no rulesets returns an empty list, so a 404 is a failed call and raises.
    live_list = gh(f"repos/{run.slug}/rulesets?per_page=100") or []
    findings = []
    for name, payload in expect.items():
        ids = [r["id"] for r in live_list if r["name"] == name]
        if not ids:
            findings.append(("DEFECT", f"ruleset: {name} missing"))
            continue
        if len(ids) > 1:
            findings.append(
                ("DRIFT", f"ruleset: {len(ids)} rulesets named {name} (resolve the duplicates)")
            )
        live_rs = gh(f"repos/{run.slug}/rulesets/{ids[0]}")
        if normalize_ruleset(live_rs) != normalize_ruleset(payload):
            source = dev_payload if name == "develop" else "repo-config/main.json"
            findings.append(("DEFECT", f"ruleset: {name} diverges from {source} (normalized diff)"))
    for stray in [r["name"] for r in live_list if r["name"] not in expect]:
        findings.append(("DRIFT", f"ruleset: stray ruleset '{stray}'"))
    return findings


def check_python_directories_declared(run):
    return python_type_findings(run.entry, run.tree)


def check_python_directories_coverage(run):
    return python_directory_coverage_findings(run.entry, run.tree)


def check_python_directories_root(run):
    return python_undeclared_root_findings(run.entry, run.tree)


def check_filenaming_scripts(run):
    return file_naming_findings(run.entry, blob_field(run.blobs, 1), run.read_text)


def check_secrets_stems(run):
    return secret_stem_findings(run.tree)


def check_secrets_names(run):
    """Secret names per store, never values, against what the applicable mechanisms require and forbid.

    The codecov mechanism follows the coverage-claiming types, so a language with no tests requires no token.
    """
    secrets = run.spec["secrets"]
    stores = {}
    # No ok404, since an empty store returns an empty list and a 404 is a failed call.
    for store in ("actions", "dependabot"):
        data = gh(f"repos/{run.slug}/{store}/secrets?per_page=100")
        stores[store] = {s["name"] for s in (data or {}).get("secrets", [])}
    mechanisms = [
        secrets["targetMechanisms"].get(p.get("target")) for p in run.entry.get("publish", [])
    ]
    for name in declared_types(run.entry):
        mechanism = secrets.get("typeMechanisms", {}).get(name)
        if mechanism != "codecov" or name in run.coverage_types:
            mechanisms.append(mechanism)
    claimed = [secrets["mechanisms"][m] for m in mechanisms if m and m in secrets["mechanisms"]]
    required = {"actions": set(), "dependabot": set()}
    for store in secrets["baseline"].get("stores", []):
        required[store] |= set(secrets["baseline"].get("requires", []))
    for mech in claimed:
        for store in mech.get("stores", []):
            required[store] |= set(mech.get("requires", []))
    # The registry requiredSecrets are the domain-specific additions, per STANDUP.md, expected in the actions store.
    required["actions"] |= set(run.entry.get("requiredSecrets", []))
    forbidden = set(secrets["baseline"].get("forbids", []))
    for mech in claimed:
        forbidden |= set(mech.get("forbids", []))
    findings = [
        ("DEFECT", f"secrets: {name} missing from the {store} store")
        for store, names in required.items()
        for name in sorted(names - stores[store])
    ]
    claimed_names = required["actions"] | required["dependabot"]
    for store, present in stores.items():
        for name in sorted(present & forbidden):
            findings.append(("DEFECT", f"secrets: forbidden {name} present in the {store} store"))
        for name in sorted(present - claimed_names):
            findings.append(
                (
                    "DRIFT",
                    f"secrets: {name} in the {store} store is claimed by no applicable mechanism (stale?)",
                )
            )
    return findings


def check_dependabot_ecosystems(run):
    """An ecosystem the tree implies, github-actions for workflows and devcontainers for a .devcontainer, is declared.

    Presence only. An absent dependabot.yml is already a file LETTER.
    """
    text = contents_text(
        gh(f"repos/{run.slug}/contents/.github/dependabot.yml?ref={run.ground}", ok404=True)
    )
    if not text:
        return []
    declared = dependabot_ecosystems(text)
    implied = {}
    workflows = gh(f"repos/{run.slug}/contents/.github/workflows?ref={run.ground}", ok404=True)
    if isinstance(workflows, list) and any(
        e["name"].endswith((".yml", ".yaml")) for e in workflows
    ):
        implied["github-actions"] = ".github/workflows/ is present"
    if gh(f"repos/{run.slug}/contents/.devcontainer?ref={run.ground}", ok404=True) is not None:
        implied["devcontainers"] = ".devcontainer/ is present"
    return [
        (
            "DRIFT",
            f"dependabot: {eco} ecosystem not declared though {why}; add it for both main and develop per the fleet norm",
        )
        for eco, why in sorted(implied.items())
        if eco not in declared
    ]


def check_files_presence(run, path):
    content, text, findings = baseline_file_findings(
        run.slug, run.ground, path, run.alternatives.get(path, []), run.baseline[path][0], run.hub
    )
    if content is not None:
        run.content[path] = text
    return findings


def _fidelity(run, path):
    item = run.baseline[path][0]
    return item, (item or {}).get("fidelity", "presence")


def check_interface_contract(run, path):
    item, fid = _fidelity(run, path)
    if fid != "interface":
        return []
    if run.content[path] is None:
        return [run.unread("interface", path, "verify its contract")]
    return check_interface(path, item.get("contract", {}), run.content[path])


def check_interface_python_directories(run, path):
    if _fidelity(run, path)[1] != "interface" or run.content[path] is None:
        return []
    return python_directories_caller_findings(path, run.content[path], run.entry)


def check_interface_concurrency(run, path):
    if _fidelity(run, path)[1] != "interface" or run.content[path] is None:
        return []
    return concurrency_caller_findings(path, run.content[path])


def check_verbatim_jobs(run, path):
    item, fid = _fidelity(run, path)
    if fid != "interface" or run.content[path] is None:
        return []
    return [
        finding
        for job in item.get("contract", {}).get("verbatimJobs", [])
        for finding in check_verbatim(
            f"{path} job '{job}'",
            run.content[path],
            item.get("reference") or path,
            extract=lambda t, j=job: split_jobs(t).get(j),
        )
    ]


def check_verbatim_file(run, path):
    item, fid = _fidelity(run, path)
    if fid != "verbatim":
        return []
    if run.content[path] is None:
        return [run.unread("verbatim", path, "compare")]
    return check_verbatim(path, run.content[path], item.get("reference") or path)


def _sections(run, path):
    """The sections a Markdown file owes here, `(presence, verbatim)`, both empty for any other file."""
    _, needed, verbatim = run.baseline[path]
    return (needed, verbatim) if path.endswith(".md") else (set(), set())


def check_sections_presence(run, path):
    needed, verbatim = _sections(run, path)
    if not (needed or verbatim):
        return []
    if run.content[path] is None:
        return [run.unread("section", path, "verify sections")]
    present = heading_texts(run.content[path])
    return [
        (
            "DRIFT",
            f"section: '{name}' not found as a heading in {path} on {run.ground} (renamed or missing; verify intent per AUDIT.md section 7)",
        )
        for name in sorted(needed)
        if name.strip().lower() not in present
    ]


def check_sections_verbatim(run, path):
    needed, verbatim = _sections(run, path)
    if not (needed or verbatim) or run.content[path] is None:
        return []
    return [
        finding
        for name in sorted(verbatim)
        for finding in check_verbatim(
            f"{path} section '{name}'",
            run.content[path],
            path,
            extract=lambda t, n=name: extract_section(t, n),
        )
    ]


def check_sections_undeclared(run, path):
    """Advisory: an undeclared H2 is a candidate duplicate of a verbatim section, or content to relocate."""
    needed, verbatim = _sections(run, path)
    if not (needed or verbatim) or run.content[path] is None:
        return []
    if path not in UNDECLARED_HEADING_SCANNED:
        return []
    return [
        (
            "DRIFT",
            f"section: '{h}' in {path} is not a declared section - reconcile it (a duplicate of a verbatim section, or repo-specific content that moves to a topical doc), or confirm it is intentional (spec/section-model.md)",
        )
        for h in undeclared_h2_headings(run.content[path], needed | verbatim)
    ]


def check_carried_template_ref(run, path):
    if path not in TEMPLATE_REF_SCANNED:
        return []
    if run.content[path] is None:
        return [run.unread("carried", path, "scan for a coordination reference")]
    if not template_ref_outside_verbatim(run.content[path], run.baseline[path][2], hub_repo_name()):
        return []
    return [
        (
            "DRIFT",
            f"carried: {path} references the template repo by name or link outside its verbatim sections (the coordination flow is machinery this repo's readers should not see; state the behavior, not the destination)",
        )
    ]


def check_carried_version_literal(run, path):
    if path not in VERSION_LITERAL_SCANNED:
        return []
    if run.content[path] is None:
        return [run.unread("carried", path, "scan for a version literal")]
    # The hub is scanned whole, since its verbatim sections are the copies every repo carries.
    excised = set() if run.hub else run.baseline[path][2]
    literals = version_literals_outside_verbatim(run.content[path], excised)
    if not literals:
        return []
    return [
        (
            "DRIFT",
            f"carried: {path} names a three-part version or a commit SHA, full or abbreviated ({', '.join(literals)}); name a pin by its mechanism, never its value, and write a versioning example with a placeholder such as 1.0.N (GOVERNANCE.md, Documentation Style Conventions, References)",
        )
    ]


def check_tree_readable(run):
    if run.entries is not None:
        return []
    return [
        (
            "DRIFT",
            f"tree: could not read the file tree for {run.slug}@{run.ground} in full, so verbatim trees, script file names, and secret-file stems are undecided",
        )
    ]


def check_tree_verbatim(run):
    if run.entries is None:
        return []
    findings = []
    for declaration in run.spec["files"].get("trees", []):
        if not applies(declaration.get("appliesTo", "*"), run.sel):
            continue
        source_root = declaration["source"].rstrip("/")
        target_root = declaration["target"].rstrip("/")
        source_files = {
            path.removeprefix(source_root + "/"): path
            for path in hub_tracked()
            if path.startswith(source_root + "/")
            and tree_path_included(path.removeprefix(source_root + "/"), declaration["include"])
        }
        target_files = {
            path.removeprefix(target_root + "/"): path
            for path in run.entries
            if path.startswith(target_root + "/")
        }
        for relative in sorted(set(source_files) - set(target_files)):
            findings.append(("LETTER", f"tree: {target_root}/{relative} is absent on {run.ground}"))
        for relative in sorted(set(source_files) & set(target_files)):
            findings.extend(
                _tree_file_findings(source_files[relative], target_files[relative], run.entries)
            )
        if declaration.get("prune"):
            for relative in sorted(set(target_files) - set(source_files)):
                findings.append(
                    ("DRIFT", f"tree: {target_root}/{relative} is extra under a pruned target")
                )
    return findings


def _tree_file_findings(source_path, target_path, entries):
    try:
        source_sha = canonical_blob_sha(source_path)
    except OSError as exc:
        return [
            ("DRIFT", f"tree: canonical {source_path} is unreadable, comparison undecided: {exc}")
        ]
    target_sha = entries[target_path]
    if source_sha == target_sha:
        return []
    if not target_sha:
        return [
            (
                "DRIFT",
                f"tree: {target_path} differs but its blob identity is unreadable, comparison undecided",
            )
        ]
    if git_blob_in_file_history(source_path, target_sha):
        return [
            ("DRIFT", f"tree: {target_path} matches a past hub revision, not the current canonical")
        ]
    return [
        (
            "DRIFT",
            f"tree: {target_path} differs from the canonical and matches no past hub revision",
        )
    ]


def check_hubonly_carried(run):
    """A path the hub tracks and the manifest omits is hub-hosted, so a downstream copy converges by deletion."""
    if run.tree is None:
        return [
            (
                "DRIFT",
                f"hub-only: could not read the file tree for {run.slug}@{run.ground} in full, so no hub-hosted file this repo carries was checked; verify by hand",
            )
        ]
    disp = gap_dispositions(run.spec)
    findings = []
    for path in sorted(run.tree & hub_only_paths(run.spec)):
        kind = disp.get(path, ("", ""))[0]
        if kind == "accepted":
            continue
        if kind == "retire":
            text = f"hub-only: delete this repo's {path} - the hub hosts it and no repo carries it (spec/divergences.json 'gaps' records why, and what to reach instead)"
        elif kind:
            text = f"hub-only: {path} is undeclared in spec/files.json and this repo carries it - the ledger dispositions it '{kind}', so settle that before converging"
        else:
            text = f"hub-only: {path} is undeclared in spec/files.json and this repo carries it - read the file and triage it in spec/divergences.json 'gaps' (this repo's own content at a shared path, a carry to declare, or a hub copy to delete)"
        findings.append(("DRIFT", text))
    return findings


def check_history_mirror(run):
    """HISTORY.md opens as the README's twin, the same H1 and the same tagline (spec/readme-structure.md)."""
    if len(run.docs) < 2:
        return []
    r_title, r_intro = title_and_intro(run.docs["README.md"])
    h_title, h_intro = title_and_intro(run.docs["HISTORY.md"])
    if r_title != h_title:
        return [
            (
                "LETTER",
                f"history: HISTORY.md title '{h_title}' does not match README.md title '{r_title}' - the changelog opens as the README's twin (spec/readme-structure.md)",
            )
        ]
    if tagline(r_intro) != tagline(h_intro):
        return [
            (
                "LETTER",
                "history: HISTORY.md tagline does not mirror the README tagline - copy the README's first line after the H1 (spec/readme-structure.md)",
            )
        ]
    return []


def check_readme_description(run):
    return description_findings(run.docs, run.entry, run.live, run.slug)


def _readme(run):
    """The README text where the README model is loaded and the README was read, else None."""
    return run.docs.get("README.md") if run.spec.get("readme") else None


def check_readme_sections(run):
    text = _readme(run)
    if text is None:
        return []
    return readme_section_findings(
        text, run.spec["readme"], run.sel, not run.live.get("private", False)
    )


def check_readme_shields(run):
    text = _readme(run)
    return [] if text is None else readme_shield_findings(text, run.spec["readme"], run.entry)


def check_readme_links(run):
    text = _readme(run)
    return [] if text is None else readme_link_findings(text, run.spec["readme"], run.slug)


def check_readme_tools(run):
    text = _readme(run)
    if text is None or not run.spec.get("tools"):
        return []
    return third_party_tool_findings(text, run.spec["tools"])


def check_cspell_workspace(run):
    """cspell.json is the one word list, so a workspace file's own cSpell list duplicates it."""
    if gh(f"repos/{run.slug}/contents/cspell.json?ref={run.ground}", ok404=True) is None:
        return []
    findings = []
    for it in gh(f"repos/{run.slug}/contents/?ref={run.ground}", ok404=True) or []:
        ws_name = it.get("name", "") if isinstance(it, dict) else ""
        if not ws_name.endswith(".code-workspace"):
            continue
        ws_text = contents_text(
            gh(f"repos/{run.slug}/contents/{ws_name}?ref={run.ground}", ok404=True)
        )
        if ws_text is None:
            findings.append(
                (
                    "DRIFT",
                    f"cspell: could not read {ws_name} on {run.ground} to check for a duplicated word list; verify by hand",
                )
            )
        elif workspace_cspell_words(ws_text):
            findings.append(
                (
                    "LETTER",
                    f"cspell: {ws_name} carries a cSpell word list while cspell.json is the single source of truth - delete the workspace copy (CODESTYLE.md Markdown and Spelling)",
                )
            )
    return findings


def check_registry_driftnotes(run):
    return driftnote_findings(run.entry, run.spec, len(run.findings))


_IS_WINDOWS = sys.platform == "win32"


def _selftest():
    """Run the self-test with no variable naming a repository in the environment.

    A git hook sets `GIT_DIR`. A case building a temporary repository would otherwise build it inside the hook's own repository.
    """
    with git_location_cleared():
        return _selftest_cases()


def _selftest_cases():
    """A smoke check of each helper family on constructed fixtures; tests/test_audit_helpers.py holds the suite."""
    pr = "name: T\non: pull_request\njobs:\n  check:\n    name: Check job\n    runs-on: ubuntu-latest\n"
    contract = {"requiredJobKeys": ["check"], "requiredCheckName": "Check job"}
    publisher = (
        "on:\n  push:\nconcurrency:\n  group: ${{ github.workflow }}-${{ github.ref }}\n"
        "  cancel-in-progress: false\njobs:\n  validate:\n    uses: o/h/.github/workflows/validate-task.yml@x\n"
        "    with:\n      python-directories: |\n        scripts\n"
    )
    doc = "# T\n\nTagline [link](https://example.invalid).\n\n## A\n\n```\n## B\n```\n\n## C\n"
    cases = [
        ("a conformant workflow", len(check_interface("wf", contract, pr)), 0),
        ("a workflow missing its job", len(check_interface("wf", contract, "jobs: {}\n")), 2),
        (
            "a pinned concurrency group",
            concurrency_caller_findings(".github/workflows/publish-release.yml", publisher),
            [],
        ),
        (
            "a declared python directory",
            python_directories_caller_findings(
                ".github/workflows/publish-release.yml",
                publisher,
                {"types": ["python"], "pythonDirectories": ["scripts"]},
            ),
            [],
        ),
        (
            "a fenced heading is not a boundary",
            extract_section(doc, "A"),
            "## A\n\n```\n## B\n```\n",
        ),
        ("a stale verbatim copy", classify_verbatim("v1\n", "v2\n", ["v1\r\n"]), "stale"),
        ("a link reduced to its text", strip_md_links("[a](u) b"), "a b"),
        ("the title and intro", title_and_intro(doc)[0], "T"),
        ("an undeclared heading", undeclared_h2_headings(doc, ["a"]), ["c"]),
        ("a version literal", version_literals_outside_verbatim("uses 1.2.3\n", []), ["1.2.3"]),
        ("a secret-file stem", len(secret_stem_findings({".secrets/Bad.txt"})), 1),
        ("the selftest flag", parse_args(["--selftest"]).selftest, True),
    ]
    ok = True
    for label, got, want in cases:
        ok = ok and got == want
        print(f"  {'ok  ' if got == want else 'FAIL'} {label}: got {got!r}")
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


def render_issue(entry, findings, ground, audited_sha, run_utc, hub_sha):
    """A ready-to-file convergence issue (title, body) generated from one repo's audit findings.

    The content is a view over the audit, not composed by hand, so it is always accurate and regenerable -
    re-run the audit, re-generate the issue. Findings are grouped by what the maintainer does with them:
    presence/contract findings to fix, drift to converge (re-vendor or review), and anything unverifiable.
    """
    name = entry["name"]
    types = ", ".join(declared_types(entry)) or "untyped"
    stamp = f"{ground}@{audited_sha[:7]}" if audited_sha else ground
    blocking = [(k, t) for k, t in findings if k in ("DEFECT", "LETTER")]
    drift = [t for k, t in findings if k == "DRIFT"]
    errors = [t for k, t in findings if k == "ERROR"]
    title = f"Converge {name} with the hub baseline ({len(findings)} finding{'' if len(findings) == 1 else 's'})"

    out = []
    w = out.append
    # No H1 - GitHub renders the issue title as the heading, so an H1 here would duplicate it.
    w(
        f"Generated from the hub audit of `{name}` ({types}). Run stamp `audit run {run_utc} | hub {hub_sha}`, "
        f"against `@ {stamp}` (the format AUDIT.md section 8 says a derived artifact quotes). Regenerate with "
        f"`spec/audit.py --issue {name}`. Findings are a point-in-time snapshot - re-run the audit before acting. "
        f"This lists what the audit mechanically detects. No check belonging to a project type in `spec/project-types.json` is run "
        f"here, and the cross-cutting dimensions are covered only in part, so the full letter and intent verdict lives in AUDIT.md section 4."
    )
    w("")
    if not findings:
        w("The deterministic checks are clean - nothing to converge.")
        return title, "\n".join(out) + "\n"
    if blocking:
        w("## Must fix")
        w("")
        w("A missing carried file, or a broken workflow contract.")
        w("")
        for k, t in blocking:
            w(f"- **{k}** {t}")
        w("")
    if drift:
        w("## Converge")
        w("")
        w(
            "Divergences from the hub canonical. A `verbatim:` file or section re-vendors the current hub copy "
            "byte-for-byte (a `section` re-vendors just that one `## heading` block). An `interface:` item must "
            "honor the named workflow contract. A stale-but-present copy is re-vendored. A `hub-only:` item is the "
            "opposite remedy, a file the hub hosts rather than carries, so the copy here is deleted and the hub's "
            "is reached instead. A genuinely repo-specific difference is judged by meaning per AUDIT.md."
        )
        w("")
        for t in drift:
            w(f"- {t}")
        w("")
    if errors:
        w("## Could not verify")
        w("")
        w("The audit could not read something it needed. Re-run once the cause is cleared.")
        w("")
        for t in errors:
            w(f"- {t}")
        w("")
    return title, "\n".join(out).rstrip() + "\n"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Live fleet audit: the deterministic subset of AUDIT.md."
    )
    ap.add_argument(
        "names", nargs="*", metavar="RepoName", help="cataloged repos to audit (default: every one)"
    )
    ap.add_argument(
        "--branch",
        metavar="REF",
        help="read this branch instead of each repo's registry groundTruthBranch, so a "
        "convergence can be verified before it is promoted; read-only, the registry is unchanged",
    )
    ap.add_argument(
        "--issue", action="store_true", help="emit one repo's convergence issue on stdout"
    )
    ap.add_argument("--selftest", action="store_true", help="run the offline engine self-test")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    if a.selftest:
        return _selftest()
    # The override reaches the same path segment and the same `?ref=` value the registry's own groundTruthBranch does,
    # so it is held to the same grammar. Validating only the declared value would leave `--branch 'main?per_page=1'`
    # retargeting every read, which is the request-goes-elsewhere shape rather than a request that fails.
    if a.branch is not None and not validate.GROUND_TRUTH_BRANCH_RE.search(a.branch):
        shape = validate.GROUND_TRUTH_BRANCH_SHAPE
        print(f"--branch {a.branch!r} does not address unencoded ({shape})", file=sys.stderr)
        return 2
    spec = {
        "registry": load("registry/repos.json"),
        "settings": load("repo-config/settings.json"),
        "secrets": load("spec/secrets.json"),
        "files": load("spec/files.json"),
        "types": load("spec/project-types.json"),
        "readme": load("spec/readme-sections.json"),
        "tools": load("spec/third-party-tools.json"),
        "divergences": load("spec/divergences.json"),
    }
    issue_mode = a.issue
    # Read once here, since both the issue path and the sweep resolve a ground-truth branch through it.
    reg_defaults = spec["registry"].get("defaults", {})
    wanted = {n.casefold() for n in a.names}
    repos = [r for r in spec["registry"]["repos"] if r.get("status") == "cataloged"]
    if wanted:
        # Keyed on the entry name rather than the owner/repo identity, and a name is unambiguous because spec/validate.py refuses one that duplicates another up to case, when it is run.
        # Casefolded rather than lowercased, and identically on both sides, because the two differ on a name carrying a sharp s: lowercasing keeps the character where casefolding spells it as a double s.
        # A name declared with one and typed with the other would match under one normalizer and not the other, so both sides use the one spec/validate.py dedupes on.
        repos = [r for r in repos if r["name"].casefold() in wanted]
        missing = wanted - {r["name"].casefold() for r in repos}
        if missing:
            print(f"Not cataloged: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    # Findings are a point-in-time snapshot.
    # Stamp the run so anything derived from it, an onboarding issue or a report, carries its own freshness signal and a reader can tell whether it still applies.
    run_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    hub = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )
    hub_sha = hub.stdout.strip() if hub.returncode == 0 else "unknown"

    # --issue <repo>: emit only the convergence issue on stdout (title first line, then body) so it captures cleanly.
    if issue_mode:
        if len(repos) != 1:
            print("--issue takes exactly one cataloged repo", file=sys.stderr)
            return 2
        entry = repos[0]
        try:
            findings, audited_sha = audit_repo(entry, spec, a.branch)
        except ShallowCloneError as e:
            print(e, file=sys.stderr)
            return 2
        # An unverifiable audit still produces an honest issue.
        except Exception as e:  # noqa: BLE001
            findings, audited_sha = [("ERROR", str(e))], ""
        title, body = render_issue(
            entry,
            findings,
            ground_branch_of(entry, a.branch, reg_defaults),
            audited_sha,
            run_utc,
            hub_sha,
        )
        print(title)
        print(
            body, end=""
        )  # line 1 is the bare title, the body starts on line 2 - head -1 / tail -n +2 friendly
        return 0

    override = f" | branch override {a.branch}" if a.branch else ""
    print(f"audit run {run_utc} | hub {hub_sha}{override}")
    _, from_remote = hub_name()
    if not from_remote:
        print(
            f"warning: no git remote; the hub name falls back to the directory name '{hub_repo_name()}', so every check that depends on the hub's identity is unreliable in this run",
            file=sys.stderr,
        )
    print()

    hard = 0

    # Fleet-wide, not per-repo, so it runs once and only on a full sweep: a name-filtered or
    # --issue run is scoped to specific repos already in the registry and has nothing to gain
    # from re-listing every repo the owner has.
    if not wanted:
        try:
            membership = membership_findings(spec)
        # A gh failure here (auth mismatch, rate limit, network) must not abort the per-repo sweep below.
        except Exception as e:  # noqa: BLE001
            membership = [("ERROR", str(e))]
        print("== Fleet membership (registry/repos.json vs GitHub) ==")
        if not membership:
            print(
                "  clean (every non-fork owned repo has a registry entry, and archived status matches GitHub)"
            )
        for kind, text in membership:
            print(f"  {kind:6} {text}")
            if kind in ("DEFECT", "LETTER", "ERROR"):
                hard += 1
        print()

    for entry in repos:
        model = (
            entry.get("workflowModel")
            or spec["registry"].get("defaults", {}).get("workflowModel")
            or "release"
        )
        ground = ground_branch_of(entry, a.branch, reg_defaults)
        try:
            findings, audited_sha = audit_repo(entry, spec, a.branch)
        # A gh/JSON failure mid-audit must not abort the sweep.
        except Exception as e:  # noqa: BLE001
            findings, audited_sha = [("ERROR", str(e))], ""
        stamp = f" @ {ground}@{audited_sha[:7]}" if audited_sha else ""
        print(f"== {entry['name']} ({', '.join(declared_types(entry))}; {model}){stamp} ==")
        if not findings:
            print(
                "  clean (deterministic checks only; no project-type check in spec/project-types.json runs here, and the cross-cutting ones are covered only in part - AUDIT.md section 4)"
            )
        for kind, text in findings:
            print(f"  {kind:6} {text}")
            if kind in ("DEFECT", "LETTER", "ERROR"):
                hard += 1
    print(f"\n{len(repos)} repo(s) audited; {hard} defect/letter/error finding(s).")
    print(
        "Findings are a point-in-time snapshot: re-run this audit before acting on them, and quote the"
    )
    print("run stamp above in any issue derived from it (AUDIT.md section 8).")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
