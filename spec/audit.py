#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py>=4", "pyyaml>=6"]
# ///
"""Live fleet audit: the deterministic subset of AUDIT.md, run from the hub with gh and the libraries above.

Compares each cataloged registry repo against the ground truth in this repo and never modifies a target.
Findings are DEFECT, LETTER (a required file is absent, intent unverified), DRIFT (non-breaking divergence), and ERROR
(a gh call failed). It exits non-zero when any repo has a DEFECT, LETTER, or ERROR finding.

Usage: uv run --no-project --script spec/audit.py [RepoName ...] [--branch REF]
--branch overrides each repo's registry groundTruthBranch for the run.
A shallow hub clone makes a sweep report one ERROR per repo, and `--issue` exits 2 with "Run `git fetch --unshallow origin` in that checkout."
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

import validate  # sibling, import-safe (its main is guarded)
import yaml
from audit_markdown import (
    description_findings,
    heading_texts,
    readme_link_findings,
    readme_section_findings,
    readme_shield_findings,
    strip_md_links,
    tagline,
    third_party_tool_findings,
    title_and_intro,
    undeclared_h2_headings,
)
from carried_text import extract_section, normalize, strip_sections

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
# The bypass_actors field is deliberately absent, since a bypass list is a per-repository decision configure.sh leaves alone.
RULESET_SUBSET = ["name", "target", "enforcement", "conditions", "rules"]
# Phrases that assert work is still outstanding, matched against a registry driftNote.
PENDING_MARKERS = ["pending", "not yet", "owed", "todo", "still", "behind", "missing", "absent"]


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


@functools.cache
def hub_tracked(rev=None):
    """The hub's own tracked paths at the resolved `main` commit.

    A git failure raises, since an empty set would clear every hub-only finding.
    """
    walk_rev = _hub_main_rev() if rev is None else rev
    # No text=True: -z's pathnames are raw bytes, and locale decoding here would crash on an invalid byte before the NUL-split below ever runs.
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
    """Hub-tracked paths the manifest does not declare, so a downstream copy is hub-hosted content rather than a carry."""
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
    """The curated ledger's manifest-gap dispositions, as path -> (disposition, reason)."""
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
    """Every blob path on a repo branch mapped to its object SHA and file mode, or None when it cannot be read in full."""
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
    """Whether path is a C# project either named or located as a test project."""
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
    """The declared types whose profile leaves them owing coverage at all, before tests are looked for."""
    profiles = repo_profiles if isinstance(repo_profiles, dict) else {}
    return [t for t in types if t == "python" or profiles.get(t) != "lint-only"]


def coverage_claiming_types(types, type_mechanisms, tree):
    """The declared types that owe Codecov coverage, meaning the CODECOV_TOKEN secret and codecov.yml.

    `tree` is the blob path set, or None when unread, and then every candidate keeps its claim.
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

    A malformed declaration drops rather than raises, since spec/validate.py reports it.
    """
    dirs = entry.get("pythonDirectories")
    return [d for d in dirs if isinstance(d, str)] if isinstance(dirs, list) else []


def python_directory_covers(directory, path):
    """Mirrors python_directories.py's covers(): '.' covers every path, any other directory its own subtree."""
    return directory == "." or path.startswith(directory + "/")


def expected_python_directories(entry, tree):
    """The directories a caller's `python-directories` declaration is expected to resolve to.

    `tree` is the blob path set, or None when unread.
    """
    declared = python_directories_of(entry)
    if declared:
        return declared
    if tree is not None and "pyproject.toml" in tree:
        return ["."]
    return []


# The catalog's hook snippets carry this helper into repos of every language, so it is fleet tooling rather than the repo's own Python.
CARRIED_PYTHON_HELPERS = {"hub_fetch_run.py", "hub-fetch-run.py"}


def declared_types(entry):
    """The string elements of the registry entry's `types` list, or an empty list where `types` is not a list."""
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

    `tree` is the blob path set, or None when unread, which reports nothing.
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

    `tree` is the blob path set, or None when unread, which reports nothing.
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
    """DRIFT naming tracked `.py` files outside every directory the registry's declaration expects.

    `tree` is the blob path set, or None when unread, which reports nothing.
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

    `blobs` maps each blob path to its mode, or is None when unread, which reports nothing.
    `read_text` returns a path's text or None, and is called only for an extensionless executable.
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

    `tree` is the blob path set, or None when unread, which reports nothing.
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
    """The hub's git blob identity for path at the resolved `main` commit, not the working tree.

    Raises OSError where path is absent from that commit or is not a regular file there.
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
    """This repo's name on the remote, and whether it came from the remote."""
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
    """GET a REST path via gh, returning parsed JSON, or None on a 404 (when ok404) or an empty response body."""
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

    Run as the owner's own account, since a repository-scoped token returns an incomplete list with no error.
    """
    me = gh("user")
    login = me.get("login") if isinstance(me, dict) else None
    # GitHub logins are case-insensitive.
    if (login or "").lower() != owner.lower():
        raise RuntimeError(
            f"gh is authenticated as '{login}', not registry owner '{owner}'. "
            "The membership check would query the wrong account's repos."
        )
    repos, page = [], 1
    while True:
        batch = gh(f"user/repos?affiliation=owner&per_page=100&page={page}")
        # An empty response body reads as None here, per the gh() docstring above.
        if batch is None:
            raise RuntimeError(f"GitHub returned an empty repository-list response (page {page})")
        if not isinstance(batch, list) or not all(isinstance(r, dict) for r in batch):
            raise RuntimeError(f"GitHub returned an unexpected repository-list shape (page {page})")
        repos.extend(r for r in batch if not r.get("fork"))
        if len(batch) < 100:
            return repos
        page += 1


def repo_identity(url):
    """owner/repo, lowercased, parsed from a github.com URL, or None if it does not parse."""
    if not isinstance(url, str):
        return None
    slug = validate.github_identity(url.strip())
    return slug.lower() if slug is not None else None


def membership_findings(spec):
    """Fleet-wide: every non-fork repo the owner has on GitHub must have a registry entry."""
    findings = []
    # A bare-name match would treat a same-named repo under a different owner as this one's entry.
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


def normalize_ruleset(payload):
    sub = {k: payload.get(k) for k in RULESET_SUBSET}
    if isinstance(sub.get("rules"), list):
        sub["rules"] = sorted(sub["rules"], key=lambda r: json.dumps(r, sort_keys=True))
    return json.dumps(sub, sort_keys=True)


def registered_check(cid):
    """The spec/checks.json entry registering `cid`, or {} where none does."""
    return next((c for c in registered_checks() if c["id"] == cid), {})


def catalog_owner(spec, cid):
    """`(owning project type or None, whether spec/project-types.json defines the id)`."""
    types = spec.get("types")
    if not types:
        raise KeyError(
            "spec['types'] (spec/project-types.json) is required to resolve a driftNote check id"
        )
    for name, t in types.get("types", {}).items():
        if any(c.get("id") == cid for c in t.get("checks", [])):
            return name, True
    cross = (c for dim in types.get("crossCutting", {}).values() for c in dim.get("checks", []))
    return None, any(c.get("id") == cid for c in cross)


def check_id_owner(spec, cid):
    """`(owning project type or None, whether spec/project-types.json or spec/checks.json defines the id)`."""
    owner, known = catalog_owner(spec, cid)
    return owner, known or bool(registered_check(cid))


def is_suppression_id(entry, cid):
    """Whether a driftNote naming `cid` suppresses a registered check's finding on this repo, rather than naming a check to judge."""
    check = registered_check(cid)
    if "suppression" not in check:
        return False
    ending_type = check["suppression"]["endsWithType"]
    return ending_type is None or ending_type not in declared_types(entry)


def driftnote_findings(entry, spec, open_count):
    """Freshness findings over one repo's registry driftNotes, given how many findings the audit already has."""
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
            elif not catalog_owner(spec, cid)[1]:
                out.append(
                    (
                        "DRIFT",
                        f"registry: driftNote names check '{cid}', which this audit evaluates (spec/checks.json) - delete the note once that check passes: {quoted}",
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
UNRESOLVED_OWNER = "_unresolved"
UNNAMED_ENTRY = "_unnamed"


def repo_slug(entry):
    """The `<owner>/<repo>` every `repos/{slug}/...` read addresses.

    An unparseable url never raises, it answers with UNRESOLVED_OWNER and the entry's name.
    """
    slug = validate.github_identity(entry.get("url"))
    if slug is not None:
        return slug
    name = str(entry.get("name", ""))
    tail = urllib.parse.quote(name, safe="") if name.strip(".") else UNNAMED_ENTRY
    return f"{UNRESOLVED_OWNER}/{tail}"


def repo_selectors(entry, defaults):
    """The scope-selector set a files.json appliesTo is matched against (see spec/scope-model.md)."""
    sel = set(declared_types(entry))
    sel.add(entry.get("workflowModel") or defaults.get("workflowModel") or "release")
    sel.add(entry.get("releaseTrigger") or defaults.get("releaseTrigger") or "two-phase")
    # The consumerModel field has no defaults fallback, since the registry schema does not allow defaults.consumerModel and validate.py requires it on every cataloged repo.
    cm = entry.get("consumerModel")
    if cm:
        sel.add(cm)
    return sel


def applies(applies_to, sel):
    """True if an appliesTo selector applies to a repo's selector set."""
    if applies_to == "*":
        return True
    tokens = applies_to if isinstance(applies_to, list) else [applies_to]
    return bool(set(tokens) & sel)


def _section_spec(elt):
    """Normalize a sections[] entry to (name, appliesTo, fidelity)."""
    if isinstance(elt, str):
        return elt, "*", "intent"
    return elt.get("name", ""), elt.get("appliesTo", "*"), elt.get("fidelity", "intent")


def required_sections(item, sel):
    """Section names to presence-check (heading grep) for this repo, the non-verbatim sections."""
    out = []
    for elt in item.get("sections", []):
        name, sec, fid = _section_spec(elt)
        if name and fid != "verbatim" and applies(sec, sel):
            out.append(name)
    return out


def verbatim_sections(item, sel):
    """Section names marked fidelity verbatim for this repo, checked byte-for-byte against the hub canonical."""
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
    """Every version literal or commit SHA in `text` outside its verbatim sections, sorted and deduplicated."""
    return sorted(
        set(VERSION_LITERAL.findall(strip_sections(text, verbatim_names, keep_pins=True)))
    )


def template_ref_outside_verbatim(text, verbatim_names, hub_name):
    """True when `hub_name` appears in `text` outside every one of its verbatim sections."""
    return hub_name.lower() in strip_sections(text, verbatim_names).lower()


def workspace_cspell_words(text):
    """True if workspace/settings JSON carries its own cSpell word list."""
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
    """A node's keys and scalar values as lines indented per level, the shape a contract token is read in."""
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
    """Each job's own source lines, `{key: text}`, cut at the parser's marks for the verbatim job hash."""
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
            view = f"  {job}:\n{code_view(jobs[job], 2)}"
            for t in toks:
                if t not in view:
                    findings.append(
                        ("DRIFT", f"interface: {path} job '{job}' missing required '{t}'")
                    )
    for job, toks in contract.get("forbidTokensInJob", {}).items():
        if job in jobs:
            view = f"  {job}:\n{code_view(jobs[job], 2)}"
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
    """DRIFT where a caller's declared `python-directories` input disagrees with the registry's own."""
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
    """True where `on:` admits a `schedule` and no `push`, and every build-release-task.yml job requires a dispatch."""
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
    """DRIFT where the publisher or the merge-bot declares a concurrency group other than the one WORKFLOW.md pins."""
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
        return []
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
    """The package-ecosystem values a dependabot.yml declares, or None where it does not parse."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
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
    """None if the copy matches the current canonical, 'stale' if it matches a past hub revision, else 'modified'."""
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
    """Raise when `root` is a shallow clone, since every history read there is silently wrong."""
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
    """The hub's own `main`, fetched fresh from `origin` and resolved to a commit SHA."""
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

    `text` is None where rel_path is absent or not a regular file at that revision, and any other git failure raises.
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
        # A real command failure must not read as no history.
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
            raise RuntimeError(f"git ls-tree failed for {sha}:{rel_path}: {t.stderr.strip()}")
        entry = t.stdout.strip()
        mode = entry.split(None, 1)[0] if entry else None
        if mode not in ("100644", "100755"):
            out.append((date, sha, None))
            continue
        # Decode as UTF-8 with replacement to match the downstream and canonical reads.
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
    """The hub's current canonical content of rel_path, from the same history read the stale check uses.

    None if rel_path has no history at `main`, or its newest revision has no file content.
    """
    revisions = _git_revisions(rel_path)
    return revisions[0][2] if revisions else None


def _last_effective_change(revisions):
    """The (date, sha) of the newest revision whose content differs from its predecessor after normalize().

    `revisions` is newest-first (date, sha, text) triples, and an empty list returns None.
    The oldest revision answers where every later one is normalized churn, and a None text counts as a change.
    Each entry is taken as its neighbor's parent, which holds for the hub's forward-only, squash-merged history.
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
    """Whether a blob occurred in a path's hub history."""
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
    """The hub checkout's most recent EFFECTIVE change to rel_path, as (iso_date, short_sha), or None if untracked."""
    change = _last_effective_change(_git_revisions(rel_path))
    if change is None:
        return None
    date, sha = change
    return date, sha[:7]


def intent_canonical_rel(item, path):
    """The hub path an intent unit's copy is judged against for staleness."""
    # Validation in spec/validate.py shape-checks these fields, but this engine runs standalone and does not invoke it first.
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
    """Map each baseline path to the `alternatives` its entry declares, the other paths it may sit at."""
    return {
        item["path"]: list(item["alternatives"]) for item in baseline if item.get("alternatives")
    }


def contents_text(content):
    """The decoded text of a contents API response, or None where it holds no inline file content.

    An empty file decodes to an empty string.
    """
    if not isinstance(content, dict) or content.get("encoding") != "base64":
        return None
    return base64.b64decode(content["content"]).decode("utf-8", "replace")


def read_baseline_file(slug, ground, path, alternatives=()):
    """The path a baseline file was found at and its contents response, or `(path, None)`."""
    for candidate in (path, *alternatives):
        content = gh(f"repos/{slug}/contents/{candidate}?ref={ground}", ok404=True)
        if content is not None:
            return candidate, content
    return path, None


def baseline_file_findings(slug, ground, path, alternatives, item, is_hub):
    """Read one baseline file, returning `(content, text, findings)` for what reading it alone owes.

    `text` is None where the API returned no inline content, and an empty file is an empty string.
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
    """The intent-staleness advisory: a last-modified comparison, since intent has no content check."""
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
    """Compare a copy, or the region `extract` returns, against the hub canonical, classifying a mismatch."""
    # Current text and history read the same _git_revisions() call, so a copy matching main cannot mismatch its own history.
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
    """`(behind, diverged)` paths main changed since the merge-base, from {path: object-sha} maps of the three trees."""
    changed_on_main = {p for p in set(base) | set(main) if base.get(p) != main.get(p)}
    differ = [p for p in changed_on_main if main.get(p) != develop.get(p)]
    behind = sorted(p for p in differ if develop.get(p) == base.get(p))
    diverged = sorted(p for p in differ if develop.get(p) != base.get(p))
    return behind, diverged


def ground_branch_of(entry, branch=None, defaults=None):
    """The branch this audit reads, the registry's `groundTruthBranch` unless overridden.

    A declared empty string is kept for spec/validate.py to refuse, rather than defaulted away.
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
        """`{path: (item, sections, verbatim sections)}` for each baseline file applying here, in manifest order."""
        out = {}
        for item in self.spec["files"]["baseline"]:
            path = item["path"]
            if not applies(item.get("appliesTo", "*"), self.sel):
                continue
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
        """README.md and HISTORY.md as read, for the checks that compare them."""
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
    """Main-side changes develop lacks, split by direction, from the merge-base tree rather than the capped compare."""
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
    """Secret names per store, never values, against what the applicable mechanisms require and forbid."""
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
    """An ecosystem the tree implies, github-actions for workflows and devcontainers for a .devcontainer, is declared."""
    text = contents_text(
        gh(f"repos/{run.slug}/contents/.github/dependabot.yml?ref={run.ground}", ok404=True)
    )
    if not text:
        return []
    declared = dependabot_ecosystems(text)
    if declared is None:
        return [
            (
                "DRIFT",
                "dependabot: .github/dependabot.yml does not parse as YAML, so its ecosystems are unread",
            )
        ]
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
    """The sections a Markdown file owes here, `(presence, verbatim)`, empty for any other file."""
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


def _selftest_cases():
    """A smoke check of each helper family on constructed fixtures."""
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
    """A ready-to-file convergence issue (title, body) generated from one repo's audit findings."""
    name = entry["name"]
    types = ", ".join(declared_types(entry)) or "untyped"
    stamp = f"{ground}@{audited_sha[:7]}" if audited_sha else ground
    blocking = [(k, t) for k, t in findings if k in ("DEFECT", "LETTER")]
    drift = [t for k, t in findings if k == "DRIFT"]
    errors = [t for k, t in findings if k == "ERROR"]
    title = f"Converge {name} with the hub baseline ({len(findings)} finding{'' if len(findings) == 1 else 's'})"

    out = []
    w = out.append
    # No H1, since GitHub renders the issue title as the heading.
    w(
        f"Generated from the hub audit of `{name}` ({types}). Run stamp `audit run {run_utc} | hub {hub_sha}`, "
        f"against `@ {stamp}` (the format AUDIT.md section 8 says a derived artifact quotes). Regenerate with "
        f"`uv run --no-project --script spec/audit.py --issue {name}`. Findings are a point-in-time snapshot - re-run the audit before acting. "
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
        return _selftest_cases()
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
    reg_defaults = spec["registry"].get("defaults", {})
    wanted = {n.casefold() for n in a.names}
    repos = [r for r in spec["registry"]["repos"] if r.get("status") == "cataloged"]
    if wanted:
        # Keyed on the entry name rather than the owner/repo identity, and a name is unambiguous because spec/validate.py refuses one that duplicates another up to case, when it is run.
        repos = [r for r in repos if r["name"].casefold() in wanted]
        missing = wanted - {r["name"].casefold() for r in repos}
        if missing:
            print(f"Not cataloged: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

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

    # Fleet-wide, so it runs once and only on a full sweep.
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
