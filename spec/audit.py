#!/usr/bin/env python3
"""Live fleet audit: the deterministic subset of AUDIT.md, run from the hub (stdlib + gh).

Compares each cataloged registry repo against the ground truth in this repo - general settings
(repo-config/settings.json), branch rulesets (normalized diff vs the model's payloads), secret
names (spec/secrets.json; values are never read), baseline/per-type file presence and per-scope
Markdown section presence on the ground-truth branch (spec/files.json, spec/scope-model.md),
hub-hosted files a repo carries and should delete (git-tracked here and undeclared in the manifest,
triaged by spec/divergences.json), intent-staleness advisories (a carried intent file whose hub
canonical changed after the copy last did), and branch-model facts (main/develop existence, develop
behind main), and a fleet-wide membership check (every non-fork repo the registry owner has on
GitHub carries a registry/repos.json entry, and an 'archived' entry's status agrees with GitHub's
own archived flag (ptr727/ProjectTemplate#550). Owner-initiated: run it when
onboarding a repo, when drift is suspected, or before fleet-wide changes. Read-only - it never
modifies a target.

Findings: DEFECT (an applicable check fails outright), LETTER (a required file is absent - intent
unverified, judge per AUDIT.md section 7), DRIFT (non-breaking divergence, e.g. main-side
changes develop lacks, a stale secret, a registry field contradicting reality), ERROR (a gh call
failed, so the repo could not be fully audited). Exits non-zero when any repo has a DEFECT,
LETTER, or ERROR finding.

Usage: python3 spec/audit.py [RepoName ...] [--branch REF]   (default: every cataloged repo,
each read at its registry groundTruthBranch). --branch overrides that branch for the run, so a
convergence can be verified before it is promoted, without editing the registry.
"""

import argparse
import base64
import fnmatch
import functools
import hashlib
import itertools
import json
import locale
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import validate  # sibling, import-safe (its main is guarded)

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
# A driftNote may name the check that would retire it, as a parenthesized id: "(hugo.generator.pinned)".
# Deliberately unanchored.
# Both notes this form was introduced for end the sentence after the paren, so an end-anchored pattern matches neither of the two it was written to catch.
# That is the shape a matcher fails at silently: it reports nothing, and a fleet with no such note in it reports exactly the same.
CHECK_ID_RE = re.compile(r"\(([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+)\)")


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


@functools.cache
def hub_tracked(rev=None):
    """The hub's own tracked paths at the resolved `main` commit, which is what "hub-side" means
    for the hub-only comparison.

    `git ls-tree -r` at that commit rather than `git ls-files` against ROOT's checked-out index
    (a filesystem walk is avoided too, since it would pick up __pycache__ and a local .venv and
    make the result depend on working-tree state), so a path present on `main` but absent on
    `develop`, or the reverse, is not silently missed or falsely added
    (ptr727/ProjectTemplate#1017 review). Filtered to regular-file modes (100644, 100755) the same
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


def repo_tree_entries(slug, ground_head):
    """Every blob path and object SHA on a repo branch, or None when it cannot be read in full.

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
    return {n["path"]: n.get("sha") for n in tree["tree"] if n.get("type") == "blob"}


def repo_tree(slug, ground_head):
    entries = repo_tree_entries(slug, ground_head)
    return None if entries is None else set(entries)


@functools.cache
def canonical_blob_sha(path):
    """The hub's git blob identity for path, from the same resolved `main` commit
    `_git_revisions()` and `_hub_main_rev()` walk, not from ROOT's checked-out working tree,
    which is not necessarily `main` (ptr727/ProjectTemplate#1017 review). Raises OSError, matching
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
        cwd=ROOT,
        check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rstrip("/").removesuffix(".git").split("/")[-1], True
    return ROOT.name, False


HUB_NAME, HUB_NAME_FROM_REMOTE = hub_name()


def gh(path, ok404=False) -> Any:
    """GET a REST path via gh, returning parsed JSON, or None on a 404 (when ok404) or an empty response body.

    No --paginate: on object endpoints it concatenates page documents into unparseable JSON. Every
    list read here fits one page; callers pass per_page=100 where a default page could truncate.
    """
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
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


GITHUB_URL_RE = re.compile(r"^https://github\.com/([^/\s?#]+)/([^/\s?#]+?)(?:\.git)?/?$")


def repo_identity(url):
    """owner/repo, lowercased, parsed from a github.com URL, or None if it does not parse.

    The canonical identity two repos are compared by, since a bare repo name collides across owners.
    GitHub's own full_name field is already this exact shape (case-preserved).
    A trailing .git is stripped so it still resolves to the same identity full_name would.
    A query string or fragment is rejected outright rather than folded into the repo name.
    spec/validate.py rejects a url that fails to parse here at all, kept in sync with this regex.
    """
    if not isinstance(url, str):
        return None
    m = GITHUB_URL_RE.match(url.strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def membership_findings(spec):
    """Fleet-wide: every non-fork repo the owner has on GitHub must have a registry entry.

    This is the check ptr727/ProjectTemplate#550 asked for: nothing else in this file, or in
    spec/validate.py, ever looks past the registry to what actually exists, so a repo that never
    got an entry is invisible to every tool that reads it. The registry was treated as ground
    truth about existence, not just about conformance. Ownership only: a fork, or a repo the
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
    """The project type owning a check id, and whether the catalog defines the id at all.

    Returns (None, True) for a cross-cutting check, which every repo carries and no repo declares.
    The catalog is a required input rather than an optional one: resolving against an absent
    catalog would report every id as undefined, which is a louder failure than reporting none.
    """
    types = spec.get("types")
    if not types:
        raise KeyError(
            "spec['types'] (spec/project-types.json) is required to resolve a driftNote check id"
        )
    for name, t in types.get("types", {}).items():
        if any(c.get("id") == cid for c in t.get("checks", [])):
            return name, True
    for dim in types.get("crossCutting", {}).values():
        if any(c.get("id") == cid for c in dim.get("checks", [])):
            return None, True
    return None, False


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
        quoted = f'"{note[:70]}{"..." if len(note) > 70 else ""}"'
        for cid in CHECK_ID_RE.findall(note):
            owner, known = check_id_owner(spec, cid)
            if not known:
                out.append(
                    (
                        "DRIFT",
                        f"registry: driftNote names check '{cid}', which spec/project-types.json does not define - fix the id or drop the note: {quoted}",
                    )
                )
            elif owner and owner not in entry.get("types", []):
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


def repo_slug(entry):
    # The url field is https://github.com/<owner>/<repo>
    return "/".join(entry["url"].rstrip("/").split("/")[-2:])


def repo_selectors(entry, defaults):
    """The scope-selector set a files.json appliesTo is matched against (see spec/scope-model.md).

    The four namespaces - project types, workflowModel, releaseTrigger, consumerModel - are disjoint, so
    a flat token set is unambiguous. workflowModel and releaseTrigger resolve repo -> defaults -> fleet
    default (as configure.sh does). consumerModel has no fleet default - validate.py requires it on every
    cataloged repo, so a cataloged repo always contributes one.
    """
    sel = set(entry.get("types", []))
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


_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")


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


def _fence_step(line, marker, marker_len):
    """One line's effect on fence state `(marker, marker_len)`: returns the state after the line, and
    whether the line itself is a fence boundary (opens or closes one) rather than fenced or plain content.

    Fence matching follows CommonMark. A fence marker needs at most 3 leading spaces, more reads as
    content, not a boundary. A closing fence needs the same character, a run at least as long as the
    opener's, and nothing but whitespace after that run.
    A mismatched or shorter marker does not close it, such as a ~~~ example inside a ``` block, or a
    ``` inside a longer ````. Trailing text does not close it either, since an opening fence's language
    tag has no closing counterpart. A backtick fence's info string may not itself contain a backtick,
    per CommonMark, so such an opener is not a boundary either.
    """
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    char = stripped[:1]
    run = len(stripped) - len(stripped.lstrip(char)) if indent <= 3 and char in ("`", "~") else 0
    if marker is None:
        if run >= 3 and not (char == "`" and "`" in stripped[run:]):
            return char, run, True
        return None, 0, False
    if char == marker and run >= marker_len and not stripped[run:].strip():
        return None, 0, True
    return marker, marker_len, False


def extract_section(text, heading):
    """The `## <heading>` H2 section including its heading line, up to the next sibling H2 or EOF, or None if absent.

    EOL-normalized to `\\n`. The match that locates the heading is by its parsed text (the text after the `## `
    marker, case-folded and stripped of surrounding whitespace), so a re-cased heading or one with extra
    marker-gap whitespace is still found rather than read as a missing section - internal heading-text
    whitespace must match exactly. The heading line's exact bytes are then part of the hashed region, so that
    re-casing or re-spacing surfaces as drift. A nested `###` stays inside the body. A `## ` line inside a
    fenced code block, per `_fence_step`, is not a boundary. So a code sample cannot truncate the region and
    hide drift after it.
    """
    want = heading.strip().lower()
    out, capturing, marker, marker_len = [], False, None, 0
    for ln in normalize(text).split("\n"):
        stripped = ln.strip()
        marker, marker_len, boundary = _fence_step(ln, marker, marker_len)
        if not boundary and marker is None and stripped.startswith("## "):
            if capturing:
                break  # a sibling H2 ends the section
            if stripped[2:].strip().lower() == want:  # parsed heading text after the "## " marker
                capturing = True
                out.append(ln)  # include the heading so its exact bytes are part of the hash
            continue
        if capturing:
            out.append(ln)
    return "\n".join(out) if capturing else None


# Carried files scanned for a coordination reference (GOVERNANCE.md "Documentation Style Conventions").
TEMPLATE_REF_SCANNED = ("AGENTS.md", "GOVERNANCE.md", ".github/copilot-instructions.md")

# The undeclared-H2 scan reads the same set.
UNDECLARED_HEADING_SCANNED = TEMPLATE_REF_SCANNED


def strip_sections(text, names):
    """`text` with each named `## <heading>` region removed, located by position rather than by content.

    Region rules match extract_section (a fenced `## ` is not a boundary, a sibling H2 ends the region), so
    the two agree on where a section starts and stops.
    Positional removal is the point: deleting the extracted text instead would also delete an identical
    passage anywhere else in the document, including one quoted inside the prose the caller means to read.
    That failure is silent and it fails open, since the removed duplicate takes its content out of the scan.
    """
    want = {n.strip().lower() for n in names}
    out, dropping, marker, marker_len = [], False, None, 0
    for ln in normalize(text).split("\n"):
        stripped = ln.strip()
        marker, marker_len, boundary = _fence_step(ln, marker, marker_len)
        if not boundary and marker is None and stripped.startswith("## "):
            dropping = (
                stripped[2:].strip().lower() in want
            )  # a sibling H2 always ends the previous region
        if not dropping:
            out.append(ln)
    return "\n".join(out)


def undeclared_h2_headings(text, declared):
    """Level-two headings in `text` that `declared` does not name, sorted.

    `declared` is normalized here (stripped, lowercased), not trusted pre-normalized. The contract
    then holds for any caller, regardless of how its own names are cased or spaced.
    Scoped to `## ` only, the section model's unit. An H1 title or a nested H3 is not itself a
    section this check judges.
    Fence-aware via unfenced_text. Either a heading-syntax line or a `##`-prefixed shell comment, shown
    inside a fenced code sample, is not misread as a real heading.
    """
    h2s = {ln[3:].strip().lower() for ln in unfenced_text(text).split("\n") if ln.startswith("## ")}
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


def heading_texts(markdown):
    """Lowercased heading texts in a Markdown document, for case-insensitive section-presence matching."""
    return {
        m.group(1).strip().lower()
        for line in markdown.splitlines()
        for m in (_HEADING.match(line),)
        if m
    }


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _bracket_matches(text, open_char, close_char):
    """Map from each open_char index in text to the index just past its balanced close_char.

    A character class like `[^\\]]*` cannot count depth, so it stops at the first close and misses a link
    label carrying its own nested brackets, e.g. `[API [docs]](url)`. Counting only open_char/close_char
    nesting, ignoring the other bracket type, needs one such map per bracket type rather than one pass
    mixing both. Built with a single left-to-right stack pass over the whole text rather than one depth-
    counting scan per open position: re-scanning from every unmatched open is what made the previous
    version O(N^2) on a run of N unmatched opens (#1011, CodeRabbit). A close pops the most recently pushed
    open, the same pairing a fresh depth count from that open would find, so this is one pass, not N.
    An open with no closing partner, or a close with nothing open, is left out of the map, same as before.

    A backslash-escaped delimiter (`\\[`, `\\]`, `\\(`, `\\)`) is skipped rather than pushed or popped,
    matching Markdown's own escaping rule, so a literal bracket inside a label does not corrupt the nesting
    count (#1011, qodo).
    """
    stack = []
    matches = {}
    escaped = False
    for i, c in enumerate(text):
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == open_char:
            stack.append(i)
        elif c == close_char and stack:
            matches[stack.pop()] = i + 1
    return matches


def markdown_link_spans(text):
    """Yield (start, end, label) for each `[label](dest)` or `[label][ref]` use, brackets/parens balanced.

    Kept in sync with spec/validate.py's contains_description_markdown_link(), which needs the same
    balanced-nesting rule for the same reason.
    """
    bracket_close = _bracket_matches(text, "[", "]")
    paren_close = _bracket_matches(text, "(", ")")
    i, n = 0, len(text)
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        label_end = bracket_close.get(i)
        if label_end is None:
            i += 1
            continue
        label = text[i + 1 : label_end - 1]
        dest_end = paren_close.get(label_end) if label_end < n and text[label_end] == "(" else None
        if dest_end is not None:
            yield i, dest_end, label
            i = dest_end
            continue
        ref_end = bracket_close.get(label_end) if label_end < n and text[label_end] == "[" else None
        if ref_end is not None:
            yield i, ref_end, label
            i = ref_end
            continue
        # This span is not itself a link.
        # A nested bracket run starting inside it may still be one, e.g. `[[docs](url)]`.
        # Retry one character in rather than skipping past the whole span (#1011, qodo).
        i += 1


def strip_md_links(text):
    """Markdown links reduced to their text - `[text](url)` and `[text][ref]` become `text`.

    The plain-text form GOVERNANCE.md "Repository Details" says the About description carries.
    """
    spans = list(markdown_link_spans(text))
    if not spans:
        return text
    out, pos = [], 0
    for start, end, label in spans:
        out.append(text[pos:start])
        out.append(label)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def title_and_intro(text):
    """The H1 title text and the intro region before the first H2, HTML comments stripped.

    Drives the README/HISTORY mirror check (spec/readme-structure.md "HISTORY.md"): both files open with the
    same title and intro, and the README's ToC-omit comment must not read as a difference.
    """
    norm = _HTML_COMMENT.sub("", normalize(text))
    title, region, seen_h1 = None, [], False
    for ln in norm.split("\n"):
        s = ln.strip()
        if not seen_h1:
            if s.startswith("# "):
                title = s[2:].strip()
                seen_h1 = True
            continue
        if s.startswith("## "):
            break
        region.append(ln.rstrip())
    # Trim the blank lines surrounding the region but keep the interior ones, so a paragraph-boundary difference is a real difference.
    # The spec says the intro is copied verbatim.
    while region and not region[0]:
        region.pop(0)
    while region and not region[-1]:
        region.pop()
    return title, "\n".join(region)


def tagline(intro):
    """The first line of a README/HISTORY intro region - the one canonical short description.

    Per spec/readme-structure.md item 1, only this line carries the length and link-free rules and only this
    line mirrors to the About panel, the Docker Hub short description, and the HISTORY.md opening. Any further
    paragraph is free prose no mirror reads, so comparing the whole region would report a legitimate second
    paragraph as a mirror difference.
    """
    return intro.split("\n")[0]


_MD_IMAGE_REF = re.compile(r"!\[[^\]]*\]\[([^\]]+)\]")
_MD_IMAGE_INLINE = re.compile(r"!\[[^\]]*\]\((\S+?)\)")
_LINK_DEF = re.compile(r"^\[([^\]]+)\]:\s*(\S+)", re.MULTILINE)
# A URI scheme, requiring two or more characters so a `C:` drive letter is not read as one.
# Every scheme a README actually carries (mailto, ftp, ssh, git, tel, data) is longer than that.
_URI_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]+:", re.IGNORECASE)


def unfenced_text(text):
    """`text` with every fenced block removed, EOL-normalized.

    Markup shown inside a code sample is being displayed rather than used, so a `[ref]: url`, a
    `<!-- Shields -->` or an `![alt][ref]` in one is not a definition, a group, or a rendered badge. Kept as
    one helper because the checkers were fence-aware in some places and blind in others, which is the state
    that lets a document be read two ways by one audit.
    Fence matching, including boundary lines themselves, is `_fence_step`'s contract.
    """
    out, marker, marker_len = [], None, 0
    for ln in normalize(text).split("\n"):
        marker, marker_len, boundary = _fence_step(ln, marker, marker_len)
        if not boundary and marker is None:
            out.append(ln)
    return "\n".join(out)


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
        and (shield.get("requireQuery") is None or shield["requireQuery"] in url)
        and (shield.get("forbidQuery") is None or shield["forbidQuery"] not in url)
    )


def addressed_region(text, address):
    """The region a shield's `in` address names, as `Heading` or `Heading > Sub-heading`, or None if absent."""
    parts = [p.strip() for p in address.split(">")]
    body = readme_region(text, parts[0])
    if body is None or len(parts) == 1:
        return body
    want, out, capturing = parts[1].lower(), [], False
    for ln in body.split("\n"):
        s = ln.strip()
        if s.startswith("### "):
            if capturing:
                break
            capturing = s[4:].strip().lower() == want
            continue
        if capturing:
            out.append(ln)
    return "\n".join(out) if capturing else None


def ordered_headings(markdown, level):
    """Heading texts at `level` in document order, HTML comments stripped, fenced blocks skipped.

    Distinct from heading_texts, which returns an unordered set across every level: the order check needs the
    sequence, and the ToC-omit comment (`## License <!-- omit from toc -->`) must not read as a different name.
    A `## ` line inside a fenced block is not a heading, matching extract_section, so a code sample cannot
    inject a phantom section.
    """
    marker = "#" * level + " "
    out = []
    for ln in _HTML_COMMENT.sub("", unfenced_text(markdown)).split("\n"):
        s = ln.strip()
        if s.startswith(marker):
            out.append(s[len(marker) :].strip())
    return out


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


# Both outer pipes are optional because GitHub's Markdown makes them optional, so a row written without either is still a row this section is graded on.
# Requiring them skipped such a row outright, so its link, its description and its place in the ordering all went unread and the repo scored clean on a table nothing had read.
# The separating pipe is what makes a row a row, and it is the one this keeps demanding, since a line carrying none of them is prose.
_TOOL_ROW = re.compile(r"^\|?\s*\[([^\]]+)\]\[([^\]]+)\]\s*\|\s*([^|]*?)\s*(?:\||$)")
_TOOL_BULLET = re.compile(r"^[-*]\s*\[([^\]]+)\]\[([^\]]+)\]\s*(.*)$")
_DELIM_CELL = re.compile(r"^:?-{3,}:?$")


def table_cells(line):
    """The cells of a Markdown table row, or None where the line is not one.

    Both outer pipes are dropped where they are there, and neither is required, for the same reason _TOOL_ROW
    stopped requiring them.

    A separating pipe is required instead, which is what keeps a thematic break out: `---` alone carries no
    pipe and returns None here, where treating it as a one-cell delimiter row would have read the paragraph
    above it as a table header.
    """
    s = line.strip()
    if "|" not in s:
        return None
    s = s.removeprefix("|")
    s = s.removesuffix("|")
    return [c.strip() for c in s.split("|")]


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
    lines = body.split("\n")
    for i, ln in enumerate(lines):
        cells = table_cells(ln)
        if i and cells and all(_DELIM_CELL.match(c) for c in cells):
            header = table_cells(lines[i - 1]) or []
            if any(c.lower() == "license" for c in header):
                findings.append(
                    (
                        "LETTER",
                        "readme: 3rd Party Tools carries a License column - a license belongs to the dependency and is authoritative at its source, so restating it adds a maintenance obligation and no information (spec/readme-structure.md)",
                    )
                )
            continue
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
            findings.append(
                (
                    "LETTER",
                    f"readme: `{sh['in']}` carries no {sh['label']} shield ({sh['match']}), which this repo's deliverables require (spec/readme-structure.md)",
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
            if strip_md_links(intro_line) != intro_line:
                findings.append(
                    (
                        "LETTER",
                        "readme: the tagline carries Markdown links - keep it link-free plain text, it doubles as the repo About description (spec/readme-structure.md)",
                    )
                )
            readme_want = strip_md_links(intro_line).strip()
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


_JOB_KEY = re.compile(r"^([A-Za-z0-9_.\-]+):(\s.*)?$")


def split_jobs(text):
    """Slice a workflow YAML into {job_key: block_text} by the jobs-level indent, without a YAML parser.

    Structural, not semantic: find the top-level `jobs:` key, take the indent of its first child as the
    job-key indent, and cut the region at each key line at exactly that indent. A line dedented below the
    job-key indent (a sibling top-level key) ends the jobs region.
    """
    lines = text.splitlines(keepends=True)
    ji = next((i for i, ln in enumerate(lines) if re.match(r"^jobs:\s*(#.*)?$", ln)), None)
    if ji is None:
        return {}
    job_indent = None
    for ln in lines[ji + 1 :]:
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        job_indent = len(ln) - len(ln.lstrip())
        break
    if not job_indent:
        return {}
    blocks, key, cur = {}, None, []
    for ln in lines[ji + 1 :]:
        indent = len(ln) - len(ln.lstrip())
        if ln.strip() and indent < job_indent:
            if ln.lstrip().startswith("#"):
                continue  # a dedented comment is not part of any job and does not end the mapping - skip it
            break  # a sibling top-level key ends the jobs region
        m = _JOB_KEY.match(ln[job_indent:]) if indent == job_indent else None
        if m:
            if key is not None:
                blocks[key] = "".join(cur)
            key, cur = (
                m.group(1),
                [ln],
            )  # include the key line so an inline mapping on it is captured
        elif key is not None:
            cur.append(ln)
    if key is not None:
        blocks[key] = "".join(cur)
    return blocks


# A `key: |`/`key: >` block-scalar indicator, with an optional anchor (`&name`) before it.
# Anchored whole-line so `key: "a|b"` does not match.
_BLOCK_SCALAR_KEY = re.compile(r"^[^:#\n]*:\s*(&\S+\s+)?[|>][0-9+-]*\s*(#.*)?$")
# A bare `- |`/`- >` sequence item, a block scalar with no mapping key at all (a matrix string).
_BARE_BLOCK_SCALAR = re.compile(r"^(&\S+\s+)?[|>][0-9+-]*\s*(#.*)?$")
# A step's `- key: |` sequence-item prefix, stripped before matching the two patterns above.
_SEQUENCE_ITEM_PREFIX = re.compile(r"^-[ \t]+")


def _code_view(text):
    """Workflow text with comment-only lines and block-scalar bodies dropped, neither is structure.

    A carried task file documents its own contract in comments (build-release-task.yml names
    `release-asset-` and `artifact-ids:` in prose), so a raw substring search over the whole text would
    both false-pass a missing handoff and false-flag a forbidden token that appears only in a comment.
    A block-scalar string value (`name: |` followed by indented text) can hide or fake a token the same
    way, so its body is dropped too, keeping only the `key:` line itself (ptr727/ProjectTemplate#949).
    """
    out = []
    skip_indent = None
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        if skip_indent is not None:
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= skip_indent:
                skip_indent = None  # a dedent back to (or past) the key column ends the block body
            else:
                continue  # still inside the block scalar body, not real structure
        stripped = ln.lstrip()
        dash_col = len(ln) - len(stripped)
        key_col = dash_col
        dash = _SEQUENCE_ITEM_PREFIX.match(stripped)
        if dash:
            # A keyed scalar's boundary is the key's own column, not the dash's.
            # A sibling key genuinely at that column (`env:` alongside a `- run: |` step) is real structure.
            key_col += dash.end()
            stripped = stripped[dash.end() :]
        if _BLOCK_SCALAR_KEY.match(stripped):
            skip_indent = key_col
        elif dash and _BARE_BLOCK_SCALAR.match(stripped):
            # A bare `- |` has no key, so its own boundary is the dash's column, not a key past it.
            skip_indent = dash_col
        out.append(ln)
    return "\n".join(out)


def job_level_names(blocks):
    """The job-level `name:` values - a job's own direct-child name key, not a step's name.

    The ruleset-bound check is a job name, so a step happening to share the string must not satisfy it. A
    job's direct children sit at the shallowest indent of its block (below the key line); a step name is
    deeper or a `- name:` list item, so neither reaches that indent as a bare `name:`.
    """
    out = set()
    for block in blocks.values():
        body = [
            ln for ln in block.splitlines()[1:] if ln.strip() and not ln.lstrip().startswith("#")
        ]
        if not body:
            continue
        child = min(len(ln) - len(ln.lstrip()) for ln in body)
        for ln in body:
            if len(ln) - len(ln.lstrip()) == child:
                m = re.match(r"name:\s*['\"]?(.*?)['\"]?\s*$", ln[child:])
                if m:
                    out.add(m.group(1))
    return out


def check_interface(path, contract, text):
    """Verify a workflow honors its fixed contract by name and wiring, never its body (spec/fidelity-model.md).

    All findings are DRIFT: the interface is what a repo must keep, the body is owned, and a rename or a
    forked handoff is a hint to verify, not a hard failure. `verbatimJobs` is the verbatim engine's concern.
    """
    findings = []
    jobs = split_jobs(text)
    code = _code_view(text)
    for k in contract.get("requiredJobKeys", []):
        if k not in jobs:
            findings.append(("DRIFT", f"interface: {path} missing required job '{k}'"))
    name = contract.get("requiredCheckName")
    if name and name not in job_level_names(jobs):
        findings.append(
            (
                "DRIFT",
                f"interface: {path} missing the ruleset-bound check name '{name}' as a job name",
            )
        )
    tok = contract.get("artifactNameToken")
    if tok and tok not in code:
        findings.append(
            ("DRIFT", f"interface: {path} missing the '{tok}<branch>-<target>' artifact handoff")
        )
    # Token checks only apply to a job that is present, since an absent job is already reported by requiredJobKeys, so skip it rather than emit a redundant "missing token" for every token it cannot contain.
    # Scan the job's code view so a token in a comment is not read as signal.
    for job, toks in contract.get("requireTokensInJob", {}).items():
        if job in jobs:
            block = _code_view(jobs[job])
            for t in toks:
                if t not in block:
                    findings.append(
                        ("DRIFT", f"interface: {path} job '{job}' missing required '{t}'")
                    )
    for job, toks in contract.get("forbidTokensInJob", {}).items():
        if job in jobs:
            block = _code_view(jobs[job])
            for t in toks:
                if t in block:
                    # Generic rather than tied to one contract's reason.
                    # forbidTokensInJob guards more than the github-release seam.
                    # intentRef on the file's own entry routes a reader to the actual context.
                    findings.append(
                        ("DRIFT", f"interface: {path} job '{job}' uses forbidden '{t}'")
                    )
    return findings


# A `uses: <action>@<40-hex sha>` pin, plus only a trailing Dependabot version comment such as ` # v1.2.3`, where the leading `v` or digit is required.
# Dependabot bumps both per repo, so that drift is governed the way EOL is rather than being a fidelity deviation.
# It is anchored to `uses:`, so a 64-hex docker digest and a tag or branch ref such as `@v4` do not match.
# Hex is case-insensitive, and a hand-written note on a pin is not version-shaped, so it survives to be compared.
_ACTION_PIN = re.compile(r"(\buses:[ \t]*[^\s@]+)@[0-9a-fA-F]{40}(?:[ \t]+#[ \t]*v?[0-9][\w.\-]*)?")
# A workflow job's `needs:` list names the jobs it sequences after.
# In a verbatim job region a repo prunes that list to the targets it actually vendors, since a `needs` entry naming an unvendored job fails the whole workflow to load, so the list is owned per repo rather than fixed.
# Mask it in the inline, scalar and block-list forms, the same as the action pin.
# The interface contract still checks the required job keys separately.
_JOB_NEEDS = re.compile(
    r"(^[ \t]*)needs:[ \t]*"
    r"(?:\[[^\]\n]*\]"  # inline: needs: [a, b] (same line only)
    r"|[A-Za-z0-9_.\-]+[ \t]*(?=\n|$)"  # scalar: needs: a
    r"|(?:\n[ \t]+-[ \t]*[A-Za-z0-9_.\-]+[ \t]*)+)",  # block: needs:\n  - a\n  - b
    re.MULTILINE,
)


def normalize(text):
    """Reduce a carried unit to its comparable form: neutralize line endings (EOL variance is governed
    separately, not a fidelity deviation), a Dependabot-owned action pin (the 40-hex `uses: <action>@<sha>` commit
    plus its ` # vN` comment, bumped per repo), and a job's owned `needs:` list (pruned per repo to its vendored
    targets). All three are governed drift, not a fidelity deviation. This is NOT placeholder masking of declared
    per-file tokens; see spec/fidelity-model.md "Normalization".
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ACTION_PIN.sub(r"\1@<pin>", text)
    return _JOB_NEEDS.sub(r"\1needs: <needs>", text)


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


@functools.cache
def _hub_main_rev():
    """The hub's own `main`, fetched fresh from `origin` and resolved to a commit SHA.

    Reached as a checkout of one's own, fetched immediately before use, per AGENTS.md and
    ptr727/ProjectTemplate#1017, rather than trusting ROOT's checked-out branch. `git fetch`
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
        check=False,
    )
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch origin main failed in {ROOT}: {fetch.stderr.strip()}")
    resolved = subprocess.run(
        ["git", "rev-parse", "FETCH_HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
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
    `_hub_main_rev()` (ptr727/ProjectTemplate#1017) rather than the implicit HEAD of whatever
    branch ROOT has checked out. The --selftest fixtures pass an explicit `rev` to exercise a
    plain local branch in a throwaway repo with no `origin` to fetch, keeping the offline engine
    self-test offline.
    """
    walk_rev = _hub_main_rev() if rev is None else rev
    r = subprocess.run(
        ["git", "log", "--format=%cI %H", walk_rev, "--", rel_path],
        cwd=ROOT,
        capture_output=True,
        text=True,
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
    never disagree about which commit they read (ptr727/ProjectTemplate#1017 review: reading
    "current" from ROOT's working tree while history walked the resolved `main` SHA let a copy
    that matches today's main misclassify as stale against its own most recent history entry).
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
    before it (verified empirically over every intent-fidelity canonical file's full history,
    ptr727/ProjectTemplate#1014): this repo's own branching is forward-only (no back-merges from
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

    `rev` defaults to the hub's own `main` via `_hub_main_rev()` (ptr727/ProjectTemplate#1017)
    for the same reason `_git_revisions` does: ROOT's checked-out branch is not necessarily
    `main`, and a develop-only revision matching `blob_sha` must not read as "stale" against a
    hub history `main` doesn't actually contain.
    """
    walk_rev = _hub_main_rev() if rev is None else rev
    result = subprocess.run(
        ["git", "log", "--format=%H", f"--find-object={blob_sha}", walk_rev, "--", rel_path],
        cwd=ROOT,
        capture_output=True,
        text=True,
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
    classifies that class as governed per-repo drift rather than a deviation (ptr727/ProjectTemplate#735).

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
    # canonical_current_text() and git_file_history() both read the same _git_revisions() call, so a copy matching today's main can never mismatch against its own history's newest entry (ptr727/ProjectTemplate#1017 review).
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


def ground_branch_of(entry, branch=None):
    """The branch this audit reads, the registry's `groundTruthBranch` unless overridden."""
    return branch or entry.get("groundTruthBranch", "main")


def audit_repo(entry, spec, branch=None):
    findings = []  # (kind, text)
    slug = repo_slug(entry)
    types = entry.get("types", [])
    model = (
        entry.get("workflowModel")
        or spec["registry"].get("defaults", {}).get("workflowModel")
        or "release"
    )
    ground = ground_branch_of(entry, branch)

    try:
        live = gh(f"repos/{slug}")
    except RuntimeError as e:
        return [("ERROR", str(e))], ""

    # --- Branch facts ---
    branch_main = gh(f"repos/{slug}/branches/main", ok404=True)
    branch_dev = gh(f"repos/{slug}/branches/develop", ok404=True)
    main_exists, dev_exists = branch_main is not None, branch_dev is not None
    if not main_exists:
        findings.append(("DEFECT", "branch: main does not exist"))
    if bool(entry.get("hasDevelop")) != dev_exists:
        findings.append(
            (
                "DRIFT",
                f"registry: hasDevelop={entry.get('hasDevelop')} but develop {'exists' if dev_exists else 'is absent'}",
            )
        )
    # Resolve the branch every content read is keyed on, reusing what the branch facts already read.
    # An unresolvable one is an error rather than a run, since every `?ref=` read would 404 and report the whole baseline as absent, which is a flood of letters describing the ref rather than the repo.
    # The branch facts above are reported either way, since they are already read and still true.
    ground_head = {"main": branch_main, "develop": branch_dev}.get(ground)
    if ground_head is None:
        ground_head = gh(f"repos/{slug}/branches/{ground}", ok404=True)
    if ground_head is None:
        return findings + [
            (
                "ERROR",
                f"branch: ground-truth branch {ground} does not exist, so nothing could be read",
            )
        ], ""
    # Commit counts mislead here, since merge-commit promotions leave main permanently ahead while the head trees are identical, so tree equality is the no-drift fast path.
    # Where the head trees differ, an empty compare files[] means develop is merely ahead, carrying no main-side changes since the merge-base, which is normal and yields no finding and no further API calls.
    if (
        main_exists
        and dev_exists
        and branch_main["commit"]["commit"]["tree"]["sha"]
        != branch_dev["commit"]["commit"]["tree"]["sha"]
    ):
        cmp = gh(f"repos/{slug}/compare/develop...main", ok404=True)
        if cmp and cmp.get("files"):
            # A non-empty files[] signals main-side changes but is not usable directly.
            # It is blind to cherry-picked promotions, where develop may already hold identical content under different commit SHAs such as a promote branch, and it is capped at 300 entries, per #336.
            # Derive the main-side change set from the merge-base tree instead, taking paths whose object SHA, a blob or a submodule pointer, differs from base to main, additions and deletions included and with no cap.
            # Then drop paths whose objects already match at develop, since content develop already has is not content develop lacks.
            # That is three recursive tree calls, so where any tree is truncated, or unexpectedly not a dict, the filter is skipped and the compare's unfiltered count is kept, which is conservative and marked.
            trees = {
                "base": gh(
                    f"repos/{slug}/git/trees/{cmp['merge_base_commit']['commit']['tree']['sha']}?recursive=1"
                ),
                "develop": gh(
                    f"repos/{slug}/git/trees/{branch_dev['commit']['commit']['tree']['sha']}?recursive=1"
                ),
                "main": gh(
                    f"repos/{slug}/git/trees/{branch_main['commit']['commit']['tree']['sha']}?recursive=1"
                ),
            }
            if not all(isinstance(t, dict) for t in trees.values()) or any(
                t.get("truncated") for t in trees.values()
            ):
                findings.append(
                    (
                        "DRIFT",
                        f"branch: {len(cmp['files'])}+ path(s) differ between main and develop (forward-sync or reconciliation may be needed; tree unavailable or too large to classify direction)",
                    )
                )
            else:
                objs = {
                    name: {
                        e["path"]: e["sha"] for e in t["tree"] if e["type"] in ("blob", "commit")
                    }
                    for name, t in trees.items()
                }
                # Split main-side drift by direction, never flagging every difference as a develop deficit.
                # A path develop still holds at the merge-base is a genuine forward-sync gap.
                # A path both branches moved is diverged, and develop may already supersede main.
                behind, diverged = classify_branch_drift(
                    objs["base"], objs["main"], objs["develop"]
                )
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

    # --- General settings ---
    expected = dict(spec["settings"])
    expected["has_discussions"] = not live.get("private", True)
    for key in SETTINGS_KEYS + ["has_discussions"]:
        if key in expected and live.get(key) != expected[key]:
            findings.append(
                ("DEFECT", f"settings: {key} live={live.get(key)} expected={expected[key]}")
            )
    if main_exists and live.get("default_branch") != "main":
        findings.append(
            ("DEFECT", f"settings: default_branch is {live.get('default_branch')}, expected main")
        )

    # --- Rulesets ---
    dev_payload = (
        "repo-config/operational/develop.json"
        if model == "operational"
        else "repo-config/develop.json"
    )
    expect_rulesets = {"develop": load(dev_payload), "main": load("repo-config/main.json")}
    # No ok404: a repo with no rulesets returns an empty list, so a 404 means the call failed
    # (access/rename) and must surface as ERROR, not read as "no rulesets".
    live_list = gh(f"repos/{slug}/rulesets?per_page=100") or []
    live_names = [r["name"] for r in live_list]
    for name, payload in expect_rulesets.items():
        ids = [r["id"] for r in live_list if r["name"] == name]
        if not ids:
            findings.append(("DEFECT", f"ruleset: {name} missing"))
            continue
        if len(ids) > 1:
            findings.append(
                ("DRIFT", f"ruleset: {len(ids)} rulesets named {name} (resolve the duplicates)")
            )
        live_rs = gh(f"repos/{slug}/rulesets/{ids[0]}")
        if normalize_ruleset(live_rs) != normalize_ruleset(payload):
            findings.append(
                (
                    "DEFECT",
                    f"ruleset: {name} diverges from {dev_payload if name == 'develop' else 'repo-config/main.json'} (normalized diff)",
                )
            )
    for stray in [n for n in live_names if n not in expect_rulesets]:
        findings.append(("DRIFT", f"ruleset: stray ruleset '{stray}'"))

    # --- Secrets (names only) ---
    secrets = spec["secrets"]
    # The codecov coverage requirement, meaning the CODECOV_TOKEN secret and the codecov.yml file, is claimed by a type only at build profile.
    # A lint-only language has no tests and so no coverage, per spec/type-model.md.
    repo_profiles = entry.get("profiles", {})
    if not isinstance(repo_profiles, dict):
        repo_profiles = {}
    coverage_active = any(
        secrets.get("typeMechanisms", {}).get(t) == "codecov"
        and repo_profiles.get(t) != "lint-only"
        for t in types
    )
    stores = {}
    # There is no ok404 here, since an empty store returns {"secrets": []}, so a 404 or 403 from permissions or a rename must surface as ERROR rather than cascade into false missing-secret DEFECTs.
    for store, path in [
        ("actions", f"repos/{slug}/actions/secrets?per_page=100"),
        ("dependabot", f"repos/{slug}/dependabot/secrets?per_page=100"),
    ]:
        data = gh(path)
        stores[store] = {s["name"] for s in (data or {}).get("secrets", [])}
    mechanisms = [
        secrets["targetMechanisms"].get(p.get("target")) for p in entry.get("publish", [])
    ]
    mechanisms += [
        secrets.get("typeMechanisms", {}).get(t)
        for t in types
        if repo_profiles.get(t) != "lint-only"
    ]
    claimed = [secrets["mechanisms"][m] for m in mechanisms if m and m in secrets["mechanisms"]]
    required_by_store = {"actions": set(), "dependabot": set()}
    for store in secrets["baseline"].get("stores", []):
        required_by_store[store] |= set(secrets["baseline"].get("requires", []))
    for mech in claimed:
        for store in mech.get("stores", []):
            required_by_store[store] |= set(mech.get("requires", []))
    # The registry requiredSecrets entries are the domain-specific additions, per STANDUP.md, being requiredSecrets plus the implicit baseline.
    # Mechanism-mapped names already carry their stores above, and unmapped ones are expected in the actions store and count as claimed, never stale.
    required_by_store["actions"] |= set(entry.get("requiredSecrets", []))
    forbidden = set(secrets["baseline"].get("forbids", []))
    for mech in claimed:
        forbidden |= set(mech.get("forbids", []))
    for store, required in required_by_store.items():
        for name in sorted(required - stores[store]):
            findings.append(("DEFECT", f"secrets: {name} missing from the {store} store"))
    for store, present in stores.items():
        for name in sorted(present & forbidden):
            findings.append(("DEFECT", f"secrets: forbidden {name} present in the {store} store"))
        claimed_names = required_by_store["actions"] | required_by_store["dependabot"]
        for name in sorted(present - claimed_names):
            findings.append(
                (
                    "DRIFT",
                    f"secrets: {name} in the {store} store is claimed by no applicable mechanism (stale?)",
                )
            )

    # --- Dependabot ecosystem coverage ---
    # A repo's tree implies Dependabot ecosystems it must track, being github-actions where it ships workflows and devcontainers where it ships a .devcontainer.
    # Without the first, the action versions those workflows reference go stale and a merge-bot then has no PRs to auto-merge.
    # The dependabot.yml file is YAML and no stdlib parser reads it, so scan the declared package-ecosystem values by regex, anchored to the line start so a commented-out entry is not read as declared.
    # This asserts an implied ecosystem's *presence* only.
    # Whether each declared ecosystem dual-targets main and develop, which is the fleet norm, is verified by inspection rather than here.
    # It only runs where dependabot.yml exists, since its absence is already a file-presence LETTER below.
    # Language ecosystems such as nuget, uv and npm are directory-scoped and not yet cross-checked here.
    db = gh(f"repos/{slug}/contents/.github/dependabot.yml?ref={ground}", ok404=True)
    if db and db.get("content"):
        declared = set(
            re.findall(
                r'^[ \t]*-?[ \t]*package-ecosystem:[ \t]*["\']?([\w-]+)',
                base64.b64decode(db["content"]).decode("utf-8", "replace"),
                re.MULTILINE,
            )
        )
        implied = {}
        workflows = gh(f"repos/{slug}/contents/.github/workflows?ref={ground}", ok404=True)
        if isinstance(workflows, list) and any(
            e["name"].endswith((".yml", ".yaml")) for e in workflows
        ):
            implied["github-actions"] = ".github/workflows/ is present"
        if gh(f"repos/{slug}/contents/.devcontainer?ref={ground}", ok404=True) is not None:
            implied["devcontainers"] = ".devcontainer/ is present"
        for eco, why in sorted(implied.items()):
            if eco not in declared:
                findings.append(
                    (
                        "DRIFT",
                        f"dependabot: {eco} ecosystem not declared though {why}; add it for both main and develop per the fleet norm",
                    )
                )

    # --- File and section presence on the ground-truth branch ---
    # The appliesTo selector is matched against the repo's full selector set, being types, workflowModel, releaseTrigger and consumerModel, so the release and operational develop rulesets are two data entries rather than a code swap.
    # Required sections union across same-path entries.
    # A carried Markdown file must contain each heading scoped to this repo.
    # A rename reads as missing and equivalence is judged by hand, so a missing section is DRIFT, a hint to verify, and never a LETTER.
    sel = repo_selectors(entry, spec["registry"].get("defaults", {}))
    wanted_sections = {}  # path -> set of required section names, unioned across applicable entries
    verbatim_secs = {}  # path -> set of section names checked byte-for-byte against the hub canonical
    doc_texts = {}  # README.md / HISTORY.md content, retained for the mirror check
    check_item = {}  # path -> entry, for a fidelity interface/verbatim entry (last applicable wins per path)
    path_order = []
    for item in spec["files"]["baseline"]:
        if not applies(item.get("appliesTo", "*"), sel):
            continue
        path = item["path"]
        if path == "codecov.yml" and not coverage_active:
            continue  # coverage feature file: N/A when no type claims codecov at build profile (spec/type-model.md)
        if path not in wanted_sections:
            wanted_sections[path] = set()
            verbatim_secs[path] = set()
            path_order.append(path)
        wanted_sections[path].update(required_sections(item, sel))
        verbatim_secs[path].update(verbatim_sections(item, sel))
        if item.get("fidelity") in ("interface", "verbatim", "intent"):
            check_item[path] = item
    for path in path_order:
        content = gh(f"repos/{slug}/contents/{path}?ref={ground}", ok404=True)
        item = check_item.get(path)
        fid = item.get("fidelity") if item else "presence"
        if content is None:
            # An interface unit's presence is DRIFT rather than LETTER, since a workflow's naming is more variable than a carried config, so absence is a hint to verify.
            # Any other unit's absence is a file-presence LETTER.
            if fid == "interface":
                findings.append(
                    ("DRIFT", f"interface: {path} absent on {ground}, cannot verify its contract")
                )
            else:
                findings.append(
                    (
                        "LETTER",
                        f"file: {path} absent on {ground} (verify intent per AUDIT.md section 7)",
                    )
                )
            continue
        # Guard on encoding rather than truthiness, since an empty file returns encoding "base64" with an empty content that decodes to an empty string.
        # A too-large or non-inline payload returns encoding "none", where text stays None and is flagged.
        text = (
            base64.b64decode(content["content"]).decode("utf-8", "replace")
            if content.get("encoding") == "base64"
            else None
        )
        if path in ("README.md", "HISTORY.md") and text is not None:
            doc_texts[path] = text  # retained for the README/HISTORY mirror check below
        # Interface conformance (name + wiring) plus any verbatim job regions the contract pins.
        if item is not None and fid == "interface":
            if text is None:
                findings.append(
                    (
                        "DRIFT",
                        f"interface: could not read {path} content on {ground} to verify its contract (no inline content returned); verify by hand",
                    )
                )
            else:
                contract = item.get("contract", {})
                findings.extend(check_interface(path, contract, text))
                canonical_rel = item.get("reference") or path
                for job in contract.get("verbatimJobs", []):
                    findings.extend(
                        check_verbatim(
                            f"{path} job '{job}'",
                            text,
                            canonical_rel,
                            extract=lambda t, j=job: split_jobs(t).get(j),
                        )
                    )
        # Whole-file verbatim: byte-identical to the hub's canonical after EOL normalization.
        elif item is not None and fid == "verbatim":
            if text is None:
                findings.append(
                    (
                        "DRIFT",
                        f"verbatim: could not read {path} content on {ground} to compare (no inline content returned); verify by hand",
                    )
                )
            else:
                findings.extend(check_verbatim(path, text, item.get("reference") or path))
        # Intent staleness: the one advisory an intent unit gets, a last-modified comparison.
        # The hub's own copies are the canonicals, so the hub itself has nothing to trail.
        elif item is not None and fid == "intent" and entry.get("name") != HUB_NAME:
            findings.extend(
                check_intent_staleness(slug, ground, path, intent_canonical_rel(item, path), text)
            )
        # Heading-based presence is only meaningful for Markdown.
        # A "section" named on a non-md file, a tasks.json task group being one, is an intent marker judged per AUDIT.md rather than a heading grep.
        needed = wanted_sections[path]
        verbatim_needed = verbatim_secs[path]
        if (needed or verbatim_needed) and path.endswith(".md"):
            if text is None:
                # Fail loud rather than skip silently, since the contents API returned no inline content, which happens for an oversized file, a symlink or a submodule.
                # The section check could not run, so surface that rather than a false clean.
                findings.append(
                    (
                        "DRIFT",
                        f"section: could not read {path} content on {ground} to verify sections (no inline content returned); verify by hand",
                    )
                )
            else:
                present = heading_texts(text)
                for name in sorted(needed):
                    if name.strip().lower() not in present:
                        findings.append(
                            (
                                "DRIFT",
                                f"section: '{name}' not found as a heading in {path} on {ground} (renamed or missing; verify intent per AUDIT.md section 7)",
                            )
                        )
                # A verbatim section must match the hub's canonical byte-for-byte once EOL-normalized, like a verbatim file but scoped to the one `## <heading>` region.
                # A universal rule block then cannot drift or fall behind a newly added rule while its heading still passes the presence check.
                for name in sorted(verbatim_needed):
                    findings.extend(
                        check_verbatim(
                            f"{path} section '{name}'",
                            text,
                            path,
                            extract=lambda t, n=name: extract_section(t, n),
                        )
                    )
                # The undeclared-section advisory, per spec/section-model.md, treats an H2 the manifest does not declare as a candidate duplicate of a verbatim section, or as repo-specific content to relocate.
                # It is advisory only, since the reconciliation can end in promoting the rule here or relocating it, so it points at that choice and never fails.
                # It covers UNDECLARED_HEADING_SCANNED, not only AGENTS.md and GOVERNANCE.md, and never names which destination file an undeclared heading belongs in.
                # Skip the hub itself, since its copies are the source and legitimately hold hub-only sections, Repository Onboarding and Conformance being one, that are deliberately not carried.
                # A downstream repo carrying such a section is still flagged, which is the point.
                if path in UNDECLARED_HEADING_SCANNED and entry.get("name") != HUB_NAME:
                    declared = {n.strip().lower() for n in (needed | verbatim_needed)}
                    for h in undeclared_h2_headings(text, declared):
                        findings.append(
                            (
                                "DRIFT",
                                f"section: '{h}' in {path} is not a declared section - reconcile it (a duplicate of a verbatim section, or repo-specific content that moves to a topical doc), or confirm it is intentional (spec/section-model.md)",
                            )
                        )

        # --- Carried files must not reference the template repo ---
        # The coordination flow is machinery a consumer should not see, so a carried file states the behavior rather than the destination.
        # A stale "report drift upstream" paragraph once spread this way.
        # Skip the hub itself, whose own carried files are the source, where naming the repo they live in is correct.
        # A downstream repo naming it in prose it owns is still flagged, which is the point.
        # Verbatim sections are excised first, and template_ref_outside_verbatim carries why that is not a loophole.
        # Sited here, in the file loop, so the scan reuses the content already fetched for the section checks and reads the same selector-resolved verbatim list they were judged against.
        if path in TEMPLATE_REF_SCANNED and entry.get("name") != HUB_NAME:
            if text is None:
                findings.append(
                    (
                        "DRIFT",
                        f"carried: could not read {path} content on {ground} to scan for a coordination reference (no inline content returned); verify by hand",
                    )
                )
            elif template_ref_outside_verbatim(text, verbatim_secs[path], HUB_NAME):
                findings.append(
                    (
                        "DRIFT",
                        f"carried: {path} references the template repo by name or link outside its verbatim sections (the coordination flow is machinery this repo's readers should not see; state the behavior, not the destination)",
                    )
                )

    # --- Manifest-owned verbatim trees ---
    carried_entries = repo_tree_entries(slug, ground_head)
    if carried_entries is None:
        findings.append(
            (
                "DRIFT",
                f"tree: could not read the file tree for {slug}@{ground} in full, so verbatim trees are undecided",
            )
        )
    elif entry.get("name") != HUB_NAME:
        for declaration in spec["files"].get("trees", []):
            if not applies(declaration.get("appliesTo", "*"), sel):
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
                for path in carried_entries
                if path.startswith(target_root + "/")
            }
            for relative in sorted(set(source_files) - set(target_files)):
                findings.append(("LETTER", f"tree: {target_root}/{relative} is absent on {ground}"))
            for relative in sorted(set(source_files) & set(target_files)):
                source_path = source_files[relative]
                try:
                    source_sha = canonical_blob_sha(source_path)
                except OSError as exc:
                    findings.append(
                        (
                            "DRIFT",
                            f"tree: canonical {source_path} is unreadable, comparison undecided: {exc}",
                        )
                    )
                    continue
                target_path = target_files[relative]
                target_sha = carried_entries[target_path]
                if source_sha == target_sha:
                    continue
                if not target_sha:
                    findings.append(
                        (
                            "DRIFT",
                            f"tree: {target_path} differs but its blob identity is unreadable, comparison undecided",
                        )
                    )
                    continue
                verdict = (
                    "stale" if git_blob_in_file_history(source_path, target_sha) else "modified"
                )
                if verdict == "stale":
                    findings.append(
                        (
                            "DRIFT",
                            f"tree: {target_path} matches a past hub revision, not the current canonical",
                        )
                    )
                else:
                    findings.append(
                        (
                            "DRIFT",
                            f"tree: {target_path} differs from the canonical and matches no past hub revision",
                        )
                    )
            if declaration.get("prune"):
                for relative in sorted(set(target_files) - set(source_files)):
                    findings.append(
                        ("DRIFT", f"tree: {target_root}/{relative} is extra under a pruned target")
                    )

    # --- Hub-only files a repo carries and should not ---
    # The manifest declares what a repo carries, so a path the hub tracks and the manifest omits is hub-hosted content per GOVERNANCE.md "Hub-Hosted Tooling".
    # A downstream copy of one is drift whose remedy is a deletion rather than a re-vendor, which no other check reports: every check above reads a path the manifest names, so nothing looks at what a repo carries beyond the baseline.
    # Skipped for the hub, whose own tracked files are the source and are all "hub-only" by construction.
    if entry.get("name") != HUB_NAME:
        carried = None if carried_entries is None else set(carried_entries)
        if carried is None:
            findings.append(
                (
                    "DRIFT",
                    f"hub-only: could not read the file tree for {slug}@{ground} in full, so no hub-hosted file this repo carries was checked; verify by hand",
                )
            )
        else:
            disp = gap_dispositions(spec)
            for path in sorted(carried & hub_only_paths(spec)):
                kind = disp.get(path, ("", ""))[0]
                # The ledger's reason is not quoted here, since slicing a sentence out of it cuts at the first period, which lands inside a filename like GOVERNANCE.md.
                # The path names the row to read instead.
                if kind == "accepted":
                    continue  # a triaged permanent divergence, so not a finding
                if kind == "retire":
                    findings.append(
                        (
                            "DRIFT",
                            f"hub-only: delete this repo's {path} - the hub hosts it and no repo carries it (spec/divergences.json 'gaps' records why, and what to reach instead)",
                        )
                    )
                elif kind:
                    findings.append(
                        (
                            "DRIFT",
                            f"hub-only: {path} is undeclared in spec/files.json and this repo carries it - the ledger dispositions it '{kind}', so settle that before converging",
                        )
                    )
                else:
                    findings.append(
                        (
                            "DRIFT",
                            f"hub-only: {path} is undeclared in spec/files.json and this repo carries it - read the file and triage it in spec/divergences.json 'gaps' (this repo's own content at a shared path, a carry to declare, or a hub copy to delete)",
                        )
                    )

    # --- HISTORY.md mirrors the README opening ---
    # Per spec/readme-structure.md "HISTORY.md", the changelog opens as the README's twin, carrying the same H1 title and the same tagline.
    # The mirror is the tagline alone, not the whole intro region: a README may carry further clarifying paragraphs, and HISTORY.md does not repeat them.
    # It is checked only where both files were readable, since absence is already a file LETTER above.
    if "README.md" in doc_texts and "HISTORY.md" in doc_texts:
        r_title, r_intro = title_and_intro(doc_texts["README.md"])
        h_title, h_intro = title_and_intro(doc_texts["HISTORY.md"])
        if r_title != h_title:
            findings.append(
                (
                    "LETTER",
                    f"history: HISTORY.md title '{h_title}' does not match README.md title '{r_title}' - the changelog opens as the README's twin (spec/readme-structure.md)",
                )
            )
        elif tagline(r_intro) != tagline(h_intro):
            findings.append(
                (
                    "LETTER",
                    "history: HISTORY.md tagline does not mirror the README tagline - copy the README's first line after the H1 (spec/readme-structure.md)",
                )
            )

    # --- README title and intro are the one canonical short description ---
    # See description_findings() for the mirror set (README, GitHub About, Docker Hub) and the declared-field precedence.
    findings += description_findings(doc_texts, entry, live, slug)

    # --- README section order, shield classes, and license-shield placement ---
    # The readme-structure dimension, driven by the declared model in spec/readme-sections.json rather than by prose.
    # Sited after the title and intro checks so one README read serves both, and guarded on the model being loaded, since the selftest builds a spec without it.
    readme_model = spec.get("readme")
    if readme_model and "README.md" in doc_texts:
        readme_text = doc_texts["README.md"]
        findings += readme_section_findings(
            readme_text, readme_model, sel, not live.get("private", False)
        )
        findings += readme_shield_findings(readme_text, readme_model, entry)
        findings += readme_link_findings(readme_text, readme_model, slug)
        if spec.get("tools"):
            findings += third_party_tool_findings(readme_text, spec["tools"])

    # --- cspell single source of truth ---
    # Per CODESTYLE.md "Markdown and Spelling", cspell.json is the one word list, and a cSpell words block left in a *.code-workspace duplicates it and silently drifts.
    # It is checked only where cspell.json is carried, since its absence is already a file LETTER above, and a workspace list with no cspell.json is that same finding.
    if gh(f"repos/{slug}/contents/cspell.json?ref={ground}", ok404=True) is not None:
        root_entries = gh(f"repos/{slug}/contents/?ref={ground}", ok404=True) or []
        for it in root_entries:
            ws_name = it.get("name", "") if isinstance(it, dict) else ""
            if not ws_name.endswith(".code-workspace"):
                continue
            ws = gh(f"repos/{slug}/contents/{ws_name}?ref={ground}", ok404=True)
            # The isinstance guard is needed because the contents API returns a list for a directory, where .get would raise.
            ws_text = (
                base64.b64decode(ws["content"]).decode("utf-8", "replace")
                if isinstance(ws, dict) and ws.get("encoding") == "base64"
                else None
            )
            if ws_text is None:
                findings.append(
                    (
                        "DRIFT",
                        f"cspell: could not read {ws_name} on {ground} to check for a duplicated word list; verify by hand",
                    )
                )
            elif workspace_cspell_words(ws_text):
                findings.append(
                    (
                        "LETTER",
                        f"cspell: {ws_name} carries a cSpell word list while cspell.json is the single source of truth - delete the workspace copy (CODESTYLE.md Markdown and Spelling)",
                    )
                )

    # --- Registry driftNotes freshness ---
    findings.extend(driftnote_findings(entry, spec, len(findings)))

    # Stamp the commit actually read for the ground-truth branch.
    # Never fall back to another branch, since a stamp naming develop while carrying main's sha would misattribute every finding.
    audited_sha = ground_head.get("commit", {}).get("sha", "")
    return findings, audited_sha


def _selftest():
    """Exercise the interface engine on synthetic fixtures, no network - verify the mechanism, not the fleet."""
    pr_check = "  check-workflow-status:\n    name: Check pull request workflow status job\n    needs: [changes]\n    runs-on: ubuntu-latest\n"
    pr_head = "name: Test\non: pull_request\njobs:\n  changes:\n    runs-on: ubuntu-latest\n"
    pr_contract = {
        "requiredJobKeys": ["check-workflow-status"],
        "requiredCheckName": "Check pull request workflow status job",
    }
    # The validate-task.yml stub contract: a caller's validate job must exist and reach the hub task by name.
    # A token check alone would pass a stub that drops the validate job entirely, since it only runs for a job that is present.
    # Naming the validate job in requiredJobKeys too catches a dropped job on its own.
    pr_stub_contract = dict(
        pr_contract,
        requiredJobKeys=["check-workflow-status", "validate"],
        requireTokensInJob={"validate": ["validate-task.yml"]},
    )
    pr_validate_head = (
        "name: Test\non: pull_request\njobs:\n"
        "  validate:\n"
        "    name: Validate sources job\n"
        "    uses: ptr727/ProjectTemplate/.github/workflows/validate-task.yml@"
        + "a" * 40
        + " # 2.0.1\n"
    )
    pr_validate_inline = (
        "name: Test\non: pull_request\njobs:\n"
        "  validate:\n"
        "    name: Validate sources job\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo inline lint\n"
    )
    # A validate-task.yml stub's own aggregator needs the validate job, never the release-shape changes job.
    # The shared pr_check fixture needs a changes job these two fixtures do not define, so this one needs validate instead.
    pr_check_validate = "  check-workflow-status:\n    name: Check pull request workflow status job\n    needs: [validate]\n    runs-on: ubuntu-latest\n"
    gh_rel = (
        "  github-release:\n    needs: [get-version, build-widget]\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/download-artifact@v4\n        with:\n          pattern: release-asset-${{ inputs.branch }}-*\n          merge-multiple: true\n"
    )
    rel_head = (
        "name: Build Release\non:\n  workflow_call:\njobs:\n"
        "  get-version:\n    runs-on: ubuntu-latest\n    steps: []\n"
        "  build-widget:\n    runs-on: ubuntu-latest\n    steps: []\n"
    )
    rel_ok = rel_head + gh_rel
    rel_contract = {
        "requiredJobKeys": ["get-version", "github-release"],
        "artifactNameToken": "release-asset-",
        "requireTokensInJob": {"github-release": ["pattern:", "merge-multiple:"]},
        "forbidTokensInJob": {"github-release": ["artifact-ids:"]},
    }
    # The merge-bot caller stub, the shape every repo carries once the merge-bot is hub-hosted.
    bot_stub = (
        "jobs:\n"
        "  merge-bot:\n"
        "    name: Merge bot pull request job\n"
        "    uses: acme/hub/.github/workflows/merge-bot-task.yml@" + "a" * 40 + " # 2.0.1\n"
        "    secrets:\n"
        "      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}\n"
        "      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}\n"
    )
    bot_contract = {
        "requiredJobKeys": ["merge-bot"],
        "requireTokensInJob": {
            "merge-bot": ["merge-bot-task.yml", "CODEGEN_APP_CLIENT_ID", "CODEGEN_APP_PRIVATE_KEY"]
        },
    }
    # The publish-release.yml caller stub once the release chain is hub-hosted, plan, validate and publish job keys.
    # Each names its hub task by token, per docs/reusable-workflows.md "Adopting the Release Chain".
    # Validate's own token requirement is what stops a stub from satisfying the job key with a no-op job.
    publish_stub = (
        "jobs:\n"
        "  plan:\n"
        "    name: Plan release job\n"
        "    uses: acme/hub/.github/workflows/publish-plan-task.yml@" + "a" * 40 + " # 2.0.1\n"
        "    with:\n"
        "      event_name: ${{ github.event_name }}\n"
        "  validate:\n"
        "    name: Validate sources job\n"
        "    needs: [plan]\n"
        "    if: ${{ needs.plan.outputs.publish == 'true' }}\n"
        "    uses: ./.github/workflows/validate-task.yml\n"
        "  publish:\n"
        "    name: Publish project release job\n"
        "    needs: [plan, validate]\n"
        "    if: ${{ needs.plan.outputs.publish == 'true' && needs.validate.result == 'success' }}\n"
        "    uses: acme/hub/.github/workflows/build-release-task.yml@" + "a" * 40 + " # 2.0.1\n"
        "    with:\n"
        "      github: true\n"
    )
    publish_contract = {
        "requiredJobKeys": ["plan", "validate", "publish"],
        "requireTokensInJob": {
            "plan": ["publish-plan-task.yml"],
            "validate": ["validate-task.yml"],
            "publish": ["build-release-task.yml", "needs.validate.result == 'success'"],
        },
    }
    cases = [
        ("conformant PR workflow", pr_head + pr_check, pr_contract, 0),
        ("PR workflow missing the required job and its check name", pr_head, pr_contract, 2),
        (
            "PR workflow with a renamed check",
            pr_head + pr_check.replace("Check pull request workflow status job", "Renamed"),
            pr_contract,
            1,
        ),
        (
            "check name present only in a run step, not as a job name",
            pr_head
            + "  check-workflow-status:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo Check pull request workflow status job\n",
            pr_contract,
            1,
        ),
        (
            "check name present only as a step name, not the job name",
            pr_head
            + "  check-workflow-status:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Check pull request workflow status job\n        run: true\n",
            pr_contract,
            1,
        ),
        (
            "PR stub validate job reaching the hub validate-task",
            pr_validate_head + pr_check_validate,
            pr_stub_contract,
            0,
        ),
        (
            "PR stub validate job still carrying an inline lint job",
            pr_validate_inline + pr_check_validate,
            pr_stub_contract,
            1,
        ),
        (
            "PR stub dropping the validate job entirely",
            pr_head + pr_check,
            pr_stub_contract,
            1,
        ),
        ("conformant release task", rel_ok, rel_contract, 0),
        (
            "release task with an artifact-ids fork in github-release",
            rel_ok.replace(
                "          merge-multiple: true\n",
                "          merge-multiple: true\n          artifact-ids: 123\n",
            ),
            rel_contract,
            1,
        ),
        (
            "release task missing merge-multiple in github-release",
            rel_ok.replace("          merge-multiple: true\n", ""),
            rel_contract,
            1,
        ),
        (
            "release task with an owned extra leaf job",
            rel_head + "  build-extra:\n    runs-on: ubuntu-latest\n    steps: []\n" + gh_rel,
            rel_contract,
            0,
        ),
        (
            "absent job reports once, no redundant token findings",
            rel_head,
            {
                "requiredJobKeys": ["github-release"],
                "requireTokensInJob": {"github-release": ["pattern:", "merge-multiple:"]},
            },
            1,
        ),
        (
            "a forbidden token only in a comment is ignored",
            rel_ok.replace(
                "          merge-multiple: true\n",
                "          merge-multiple: true\n          # never an artifact-ids: fork here\n",
            ),
            rel_contract,
            0,
        ),
        (
            "a required token only in a comment does not count",
            rel_ok.replace(
                "          merge-multiple: true\n", "          # merge-multiple: true (was here)\n"
            ),
            rel_contract,
            1,
        ),
        (
            "merge-bot caller stub reaching the hub task with both secrets mapped",
            bot_stub,
            bot_contract,
            0,
        ),
        (
            "merge-bot caller stub with the hub's own local uses",
            bot_stub.replace(
                "acme/hub/.github/workflows/merge-bot-task.yml@" + "a" * 40 + " # 2.0.1",
                "./.github/workflows/merge-bot-task.yml",
            ),
            bot_contract,
            0,
        ),
        (
            "merge-bot copy still carrying the job bodies reports the missing caller job once",
            (
                "jobs:\n  merge-dependabot:\n    runs-on: ubuntu-latest\n    steps: []\n"
                "  disable-auto-merge-on-maintainer-push:\n    runs-on: ubuntu-latest\n    steps: []\n"
            ),
            bot_contract,
            1,
        ),
        (
            "merge-bot caller stub that maps only one secret",
            bot_stub.replace(
                "      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}\n", ""
            ),
            bot_contract,
            1,
        ),
        (
            "publish-release.yml stub reaching both hub tasks by pin",
            publish_stub,
            publish_contract,
            0,
        ),
        (
            "publish-release.yml stub with the hub's own local uses",
            publish_stub.replace(
                "acme/hub/.github/workflows/publish-plan-task.yml@" + "a" * 40 + " # 2.0.1",
                "./.github/workflows/publish-plan-task.yml",
            ).replace(
                "acme/hub/.github/workflows/build-release-task.yml@" + "a" * 40 + " # 2.0.1",
                "./.github/workflows/build-release-task.yml",
            ),
            publish_contract,
            0,
        ),
        (
            "publish-release.yml stub missing the plan job reports it once",
            "jobs:\n"
            "  validate:\n"
            "    name: Validate sources job\n"
            "    uses: ./.github/workflows/validate-task.yml\n"
            "  publish:\n"
            "    name: Publish project release job\n"
            "    needs: [validate]\n"
            "    if: ${{ needs.validate.result == 'success' }}\n"
            "    uses: acme/hub/.github/workflows/build-release-task.yml@"
            + "a" * 40
            + " # 2.0.1\n",
            publish_contract,
            1,
        ),
        (
            "publish-release.yml stub missing the validate job reports it once",
            "jobs:\n"
            "  plan:\n"
            "    name: Plan release job\n"
            "    uses: acme/hub/.github/workflows/publish-plan-task.yml@" + "a" * 40 + " # 2.0.1\n"
            "  publish:\n"
            "    name: Publish project release job\n"
            "    needs: [plan, validate]\n"
            "    if: ${{ needs.validate.result == 'success' }}\n"
            "    uses: acme/hub/.github/workflows/build-release-task.yml@"
            + "a" * 40
            + " # 2.0.1\n",
            publish_contract,
            1,
        ),
        (
            "publish-release.yml stub whose publish job never names build-release-task.yml",
            publish_stub.replace(
                "acme/hub/.github/workflows/build-release-task.yml@" + "a" * 40 + " # 2.0.1",
                "./.github/workflows/build-release-task-renamed.yml",
            ),
            publish_contract,
            1,
        ),
        (
            "publish-release.yml stub whose validate job never names validate-task.yml",
            publish_stub.replace(
                "    uses: ./.github/workflows/validate-task.yml\n",
                "    uses: ./.github/workflows/validate-task-renamed.yml\n",
            ),
            publish_contract,
            1,
        ),
        (
            "publish-release.yml stub whose publish job ignores failed validation",
            publish_stub.replace(" && needs.validate.result == 'success'", ""),
            publish_contract,
            1,
        ),
    ]
    # The deploy-site.yml caller stub once deploy-site-task.yml is hub-hosted: no secrets: inherit, the one crossing secret named instead.
    # inherit is documented for same-org or enterprise callers, so the fleet does not use it across repositories.
    # No job-level environment: on the caller, unsupported on a job with uses: (ptr727/ProjectTemplate#942).
    deploy_stub = (
        "jobs:\n"
        "  assert-ref:\n    runs-on: ubuntu-latest\n    steps: []\n"
        "  validate:\n    uses: ./.github/workflows/validate-task.yml\n"
        "  deploy:\n"
        "    name: Deploy job\n"
        "    permissions:\n      contents: read\n"
        "    uses: acme/hub/.github/workflows/deploy-site-task.yml@" + "a" * 40 + " # 2.0.1\n"
        "    with:\n      environment: ${{ inputs.environment }}\n"
        "    secrets:\n      DEPLOY_SSH_PRIVATE_KEY: ${{ secrets.DEPLOY_SSH_PRIVATE_KEY }}\n"
    )
    deploy_contract = {
        "requiredJobKeys": ["assert-ref", "validate", "deploy"],
        "requireTokensInJob": {
            "deploy": [
                "deploy-site-task.yml",
                # Anchored to the with: block specifically, not just 6-space indent.
                # A secret named "environment" would also land at that indent otherwise.
                # The task declares this input required, and a missing one fails at dispatch, not at audit time.
                "with:\n      environment:",
                "contents: read",
                "DEPLOY_SSH_PRIVATE_KEY",
            ]
        },
        "forbidTokensInJob": {
            # Indent-anchored (4 spaces) to the job's own top level, the shape GitHub rejects
            # at parse time on a job with uses: (ptr727/ProjectTemplate#942).
            "deploy": ["\n    environment:"]
        },
    }
    cases += [
        (
            "deploy-site.yml caller stub reaching the hub task with the crossing secret mapped",
            deploy_stub,
            deploy_contract,
            0,
        ),
        (
            "deploy-site.yml caller stub still using secrets: inherit reports the missing secret",
            deploy_stub.replace(
                "    secrets:\n      DEPLOY_SSH_PRIVATE_KEY: ${{ secrets.DEPLOY_SSH_PRIVATE_KEY }}\n",
                "    secrets: inherit\n",
            ),
            deploy_contract,
            1,
        ),
        (
            "deploy-site.yml caller stub reintroducing the invalid job-level environment: key",
            deploy_stub.replace(
                "    name: Deploy job\n",
                "    name: Deploy job\n    environment: ${{ inputs.environment }}\n",
            ),
            deploy_contract,
            1,
        ),
        (
            "deploy-site.yml caller stub missing the required environment input",
            deploy_stub.replace("    with:\n      environment: ${{ inputs.environment }}\n", ""),
            deploy_contract,
            1,
        ),
        (
            "deploy-site.yml caller stub with environment: under the wrong mapping still reports missing",
            # A same-indented environment: key under secrets: must not satisfy the with:-anchored requirement on its own.
            # The oddly-named secret's value is left generic so it does not incidentally satisfy the DEPLOY_SSH_PRIVATE_KEY token.
            deploy_stub.replace(
                "    with:\n      environment: ${{ inputs.environment }}\n",
                "    secrets:\n      environment: ${{ secrets.SOME_OTHER_SECRET }}\n",
            ),
            deploy_contract,
            1,
        ),
        (
            "deploy-site.yml caller stub missing the contents: read grant the task's jobs declare",
            deploy_stub.replace("    permissions:\n      contents: read\n", ""),
            deploy_contract,
            1,
        ),
        (
            # A block scalar crafted to contain the required token as string content must still report it missing.
            # The real `with:` mapping is removed here, so only the block scalar carries the text (ptr727/ProjectTemplate#949).
            "deploy-site.yml caller stub with the with:/environment: tokens only inside a block-scalar name still reports missing",
            deploy_stub.replace(
                "    name: Deploy job\n", "    name: |\n      with:\n      environment:\n"
            ).replace("    with:\n      environment: ${{ inputs.environment }}\n", ""),
            deploy_contract,
            1,
        ),
    ]
    # The stage-5 type-specific hub tasks carry no manifest entry yet, since no repo has adopted a caller stub for them.
    # These fixtures exercise the contract adoption will register, proving the interface engine reads it correctly before any downstream repo depends on that reading.
    readme_stub = (
        "jobs:\n"
        "  publish-docker-readme:\n"
        "    name: Publish Docker Hub readme job\n"
        "    permissions:\n      contents: read\n"
        "    uses: acme/hub/.github/workflows/publish-docker-readme-task.yml@"
        + "a"
        * 40
        + " # 2.0.1\n"
        "    with:\n      branch: ${{ github.ref_name }}\n"
        "    secrets:\n"
        "      DOCKER_HUB_USERNAME: ${{ secrets.DOCKER_HUB_USERNAME }}\n"
        "      DOCKER_HUB_ACCESS_TOKEN: ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}\n"
    )
    readme_contract = {
        "requiredJobKeys": ["publish-docker-readme"],
        "requireTokensInJob": {
            "publish-docker-readme": [
                "publish-docker-readme-task.yml",
                "contents: read",
                "DOCKER_HUB_USERNAME",
                "DOCKER_HUB_ACCESS_TOKEN",
            ]
        },
    }
    upstream_stub = (
        "jobs:\n"
        "  check-upstream-version:\n"
        "    name: Check upstream version job\n"
        "    uses: acme/hub/.github/workflows/check-upstream-version-task.yml@"
        + "a"
        * 40
        + " # 2.0.1\n"
        "    secrets:\n"
        "      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}\n"
        "      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}\n"
    )
    upstream_contract = {
        "requiredJobKeys": ["check-upstream-version"],
        "requireTokensInJob": {
            "check-upstream-version": [
                "check-upstream-version-task.yml",
                "CODEGEN_APP_CLIENT_ID",
                "CODEGEN_APP_PRIVATE_KEY",
            ]
        },
    }
    codegen_stub = (
        "jobs:\n"
        "  run-codegen:\n"
        "    name: Run codegen and pull request job\n"
        "    uses: acme/hub/.github/workflows/run-codegen-pull-request-task.yml@"
        + "a"
        * 40
        + " # 2.0.1\n"
        "    secrets:\n"
        "      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}\n"
        "      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}\n"
    )
    codegen_contract = {
        "requiredJobKeys": ["run-codegen"],
        "requireTokensInJob": {
            "run-codegen": [
                "run-codegen-pull-request-task.yml",
                "CODEGEN_APP_CLIENT_ID",
                "CODEGEN_APP_PRIVATE_KEY",
            ]
        },
    }
    for label_prefix, stub, contract in (
        ("publish-docker-readme.yml", readme_stub, readme_contract),
        ("check-upstream-version.yml", upstream_stub, upstream_contract),
        ("run-codegen-pull-request.yml", codegen_stub, codegen_contract),
    ):
        cases += [
            (
                f"{label_prefix} caller stub reaching the hub task with secrets mapped",
                stub,
                contract,
                0,
            ),
            (
                f"{label_prefix} copy still carrying the job body reports the missing caller job",
                "jobs:\n  some-other-job:\n    runs-on: ubuntu-latest\n    steps: []\n",
                contract,
                1,
            ),
        ]
    ok = True
    for label, text, contract, want in cases:
        got = len(check_interface("wf", contract, text))
        if got != want:
            ok = False
        print(f"  {'ok  ' if got == want else 'FAIL'} want={want} got={got}  {label}")
    trailing = split_jobs(rel_ok + "# a trailing top-level comment\n")
    if set(trailing) != {
        "get-version",
        "build-widget",
        "github-release",
    } or "trailing top-level comment" in trailing.get("github-release", ""):
        ok = False
        print(f"  FAIL split_jobs (trailing comment) -> {sorted(trailing)}")
    else:
        print(f"  ok   split_jobs (trailing comment) -> {sorted(trailing)}")
    inline = split_jobs(
        "name: X\non: push\njobs:\n  quick: {runs-on: ubuntu-latest}\n  full:\n    runs-on: ubuntu-latest\n"
    )
    if set(inline) != {"quick", "full"} or "runs-on" not in inline.get("quick", ""):
        ok = False
        print(f"  FAIL split_jobs (inline mapping) -> {sorted(inline)}")
    else:
        print("  ok   split_jobs (inline-mapping job captured with its content)")

    # _code_view()'s block-scalar handling: only the body is dropped, never the key line itself.
    code_view_cases = [
        (
            "block scalar body dropped, key line kept",
            "  deploy:\n    name: |\n      with:\n      environment:\n",
            "  deploy:\n    name: |",
        ),
        (
            "a dedented sibling key ends the block scalar body",
            "  deploy:\n    name: |\n      with:\n    with:\n      environment: prod\n",
            "  deploy:\n    name: |\n    with:\n      environment: prod",
        ),
        (
            "folded scalar, strip-chomp indicator, trailing comment on the key line",
            "  deploy:\n    name: >-  # a folded, strip-chomped scalar\n      environment:\n",
            "  deploy:\n    name: >-  # a folded, strip-chomped scalar",
        ),
        (
            # A step's `- run: |` boundary is `run:`'s column, not the dash's column.
            # A sibling `env:` key genuinely at that column is real structure, not block-scalar body.
            "a step's `- run: |` block scalar body drops, its sibling env: mapping survives",
            "      - run: |\n          echo body\n        env:\n          TOKEN: xyz\n",
            "      - run: |\n        env:\n          TOKEN: xyz",
        ),
        (
            # A valid YAML anchor property (`&label`) sits between the colon and the indicator.
            "an anchored block scalar (`name: &label |`) is still recognized as a block-scalar key",
            "  deploy:\n    name: &lbl |\n      with:\n      environment:\n",
            "  deploy:\n    name: &lbl |",
        ),
        (
            # A bare sequence item (a matrix string) has no key, so its boundary is the dash's column.
            "a bare sequence-item block scalar (`- |`, a matrix string) still drops its body",
            "  include:\n    - |\n      TOKEN\n",
            "  include:\n    - |",
        ),
    ]
    for label, text, want in code_view_cases:
        got = _code_view(text)
        if got != want:
            ok = False
            print(f"  FAIL _code_view ({label}) -> {got!r}, want {want!r}")
        else:
            print(f"  ok   _code_view ({label})")

    # The verbatim engine, covering EOL normalization, hashing, and the stale-versus-modified classification.
    # It is exercised here rather than only in production, because a latent bug in the comparison would otherwise surface as a false clean on a real fleet run.
    canon = "line one\nline two\nline three\n"
    verbatim_cases = [
        # (label, down_text, canon_text, history, want)
        ("identical -> match", canon, canon, [], None),
        ("EOL-only diff (CRLF) -> match", canon.replace("\n", "\r\n"), canon, [], None),
        ("EOL-only diff (bare CR) -> match", canon.replace("\n", "\r"), canon, [], None),
        (
            "body edit -> modified",
            canon.replace("line two", "line TWO edited"),
            canon,
            [],
            "modified",
        ),
        (
            "matches a past revision -> stale",
            "old body\n",
            "current body\n",
            ["old body\n", "older\n"],
            "stale",
        ),
        (
            "matches a past revision modulo EOL -> stale",
            "old body\r\n",
            "current body\n",
            ["old body\n"],
            "stale",
        ),
        (
            "edit in no revision -> modified",
            "never existed\n",
            "current body\n",
            ["old body\n"],
            "modified",
        ),
    ]
    for label, down, canon_t, history, want in verbatim_cases:
        got = classify_verbatim(down, canon_t, history)
        if got != want:
            ok = False
        print(
            f"  {'ok  ' if got == want else 'FAIL'} want={want!s:>8} got={got!s:>8}  verbatim: {label}"
        )
    # Action-pin neutralization, where a Dependabot uses:@<sha> bump, meaning both the 40-hex sha and its ` # vN` comment, must not count as verbatim drift, while a changed action name must.
    # This is what lets a verbatim workflow region survive routine action bumps while still catching a real fork.
    pin_a = "      - uses: actions/checkout@" + "a" * 40 + " # v7.0.0\n"
    pin_b = (
        "      - uses: actions/checkout@" + "B" * 40 + " # v7.0.1\n"
    )  # uppercase hex + version bump
    pin_struct = "      - uses: actions/setup-node@" + "a" * 40 + " # v7.0.0\n"
    note_x = "      - uses: actions/checkout@" + "a" * 40 + " # kept for the audited build\n"
    note_y = "      - uses: actions/checkout@" + "a" * 40 + " # kept for a different reason\n"
    if content_hash(pin_a) != content_hash(pin_b):
        ok = False
        print(
            "  FAIL action-pin: a uses:@<sha> bump (sha + # vN, uppercase hex) should normalize equal"
        )
    elif content_hash(pin_a) == content_hash(pin_struct):
        ok = False
        print("  FAIL action-pin: a changed action name must still hash differently")
    elif content_hash(note_x) == content_hash(note_y):
        ok = False
        print(
            "  FAIL action-pin: a hand-written (non-version) pin comment must survive to be compared"
        )
    else:
        print(
            "  ok   action-pin: version bump normalizes equal, changed action differs, hand-written note survives"
        )

    # The needs-mask case, where a verbatim job region whose `needs:` list is pruned to the repo's vendored targets must not count as drift, since the list is owned, while a structural change to the job's steps must.
    needs_full = "  github-release:\n    needs: [get-version, validate-release, build-nuget, dotnet-publish]\n    runs-on: x\n    steps: []\n"
    needs_pruned = "  github-release:\n    needs: [get-version, validate-release, dotnet-publish]\n    runs-on: x\n    steps: []\n"
    needs_block = "  github-release:\n    needs:\n      - get-version\n      - dotnet-publish\n    runs-on: x\n    steps: []\n"
    needs_scalar = "  github-release:\n    needs: dotnet-publish\n    runs-on: x\n    steps: []\n"
    needs_forked = "  github-release:\n    needs: [get-version, validate-release]\n    runs-on: x\n    steps:\n      - run: fork\n"
    if (
        len(
            {
                content_hash(needs_full),
                content_hash(needs_pruned),
                content_hash(needs_block),
                content_hash(needs_scalar),
            }
        )
        != 1
    ):
        ok = False
        print(
            "  FAIL needs-mask: a pruned needs list (inline, block, or scalar) should normalize equal"
        )
    elif content_hash(needs_full) == content_hash(needs_forked):
        ok = False
        print("  FAIL needs-mask: a step change must still hash differently")
    elif "runs-on: x" not in normalize(needs_block):
        ok = False
        print("  FAIL needs-mask: masking a block needs list must not consume the next key")
    else:
        print(
            "  ok   needs-mask: pruned needs (inline, block, scalar) normalizes equal, forked step differs, next key preserved"
        )

    # _last_effective_change must skip a normalized-only bump, per ptr727/ProjectTemplate#735.
    d3, d2, d1 = (
        "2024-03-01T00:00:00+00:00",
        "2024-02-01T00:00:00+00:00",
        "2024-01-01T00:00:00+00:00",
    )
    lec_cases = [
        (
            "every bump back to creation is pin-only -> creation wins",
            [(d3, "sha3", pin_b), (d2, "sha2", pin_a), (d1, "sha1", pin_a)],
            (d1, "sha1"),
        ),
        (
            "newest differs for real -> newest wins",
            [(d3, "sha3", pin_struct), (d2, "sha2", pin_a), (d1, "sha1", pin_a)],
            (d3, "sha3"),
        ),
        (
            "newest is a pin-only bump over a real change -> the real change wins",
            [(d3, "sha3", pin_b), (d2, "sha2", pin_a), (d1, "sha1", pin_struct)],
            (d2, "sha2"),
        ),
        (
            "one revision (creation) -> that revision wins, nothing to compare against",
            [(d1, "sha1", "only revision\n")],
            (d1, "sha1"),
        ),
        (
            "unreadable newest text -> treated as effective, never silently skipped",
            [(d2, "sha2", None), (d1, "sha1", "whatever\n")],
            (d2, "sha2"),
        ),
        ("no history -> None", [], None),
    ]
    for label, revisions, want in lec_cases:
        got = _last_effective_change(revisions)
        if got != want:
            ok = False
        print(
            f"  {'ok  ' if got == want else 'FAIL'} want={want!s:<24} got={got!s:<24}  _last_effective_change: {label}"
        )

    # _git_revisions: a deletion revision reads as None even once rel_path exists again in a
    # later commit, per ptr727/ProjectTemplate#1016 and #1018.
    with tempfile.TemporaryDirectory() as tmp_root:
        tmp_root_path = pathlib.Path(tmp_root)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@test.invalid"],
            ["git", "config", "user.name", "test"],
        ):
            subprocess.run(cmd, cwd=tmp_root_path, check=True, capture_output=True)
        rel = "deletion-probe.txt"
        (tmp_root_path / rel).write_text("v1\n")
        subprocess.run(["git", "add", rel], cwd=tmp_root_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add"], cwd=tmp_root_path, check=True, capture_output=True
        )
        subprocess.run(["git", "rm", "-q", rel], cwd=tmp_root_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "delete"],
            cwd=tmp_root_path,
            check=True,
            capture_output=True,
        )
        (tmp_root_path / rel).write_text("v2\n")
        subprocess.run(["git", "add", rel], cwd=tmp_root_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "readd"],
            cwd=tmp_root_path,
            check=True,
            capture_output=True,
        )
        global ROOT
        saved_root = ROOT
        ROOT = tmp_root_path
        try:
            _git_revisions.cache_clear()
            revisions = _git_revisions(rel, rev="HEAD")
        finally:
            ROOT = saved_root
            _git_revisions.cache_clear()
    got = [text for _, _, text in revisions]
    want = ["v2\n", None, "v1\n"]
    if got != want:
        ok = False
    print(
        f"  {'ok  ' if got == want else 'FAIL'} want={want!s:<24} got={got!s:<24}  _git_revisions: re-added file"
    )

    # _git_revisions: a path that becomes a directory reads as None too, per
    # ptr727/ProjectTemplate#1016 (git show on a tree path returns a listing, not file content).
    with tempfile.TemporaryDirectory() as tmp_root:
        tmp_root_path = pathlib.Path(tmp_root)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@test.invalid"],
            ["git", "config", "user.name", "test"],
        ):
            subprocess.run(cmd, cwd=tmp_root_path, check=True, capture_output=True)
        rel = "thing"
        (tmp_root_path / rel).write_text("v1\n")
        subprocess.run(["git", "add", rel], cwd=tmp_root_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "file"],
            cwd=tmp_root_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "rm", "-q", rel], cwd=tmp_root_path, check=True, capture_output=True)
        (tmp_root_path / rel).mkdir()
        (tmp_root_path / rel / "inner.txt").write_text("v2\n")
        subprocess.run(["git", "add", rel], cwd=tmp_root_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "now a directory"],
            cwd=tmp_root_path,
            check=True,
            capture_output=True,
        )
        saved_root = ROOT
        ROOT = tmp_root_path
        try:
            _git_revisions.cache_clear()
            revisions = _git_revisions(rel, rev="HEAD")
        finally:
            ROOT = saved_root
            _git_revisions.cache_clear()
    got = [text for _, _, text in revisions]
    want = [None, "v1\n"]
    if got != want:
        ok = False
    print(
        f"  {'ok  ' if got == want else 'FAIL'} want={want!s:<24} got={got!s:<24}  _git_revisions: file-to-directory transition"
    )

    # _git_revisions: a path that becomes a symlink reads as None too, per
    # ptr727/ProjectTemplate#1016 (a symlink's ls-tree type is "blob", but git show returns its
    # target path, not file content).
    with tempfile.TemporaryDirectory() as tmp_root:
        tmp_root_path = pathlib.Path(tmp_root)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@test.invalid"],
            ["git", "config", "user.name", "test"],
        ):
            subprocess.run(cmd, cwd=tmp_root_path, check=True, capture_output=True)
        rel = "thing"
        (tmp_root_path / rel).write_text("v1\n")
        subprocess.run(["git", "add", rel], cwd=tmp_root_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "file"],
            cwd=tmp_root_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "rm", "-q", rel], cwd=tmp_root_path, check=True, capture_output=True)
        try:
            (tmp_root_path / rel).symlink_to("target")
        except OSError:
            # Creating a symlink needs a privilege this host or user may not have (notably
            # Windows without Developer Mode or an elevated prompt): skip this case rather than
            # aborting the whole --selftest run over an environment limitation, not a code fault.
            print("  skip _git_revisions: file-to-symlink transition (host cannot create symlinks)")
        else:
            subprocess.run(["git", "add", rel], cwd=tmp_root_path, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "now a symlink"],
                cwd=tmp_root_path,
                check=True,
                capture_output=True,
            )
            saved_root = ROOT
            ROOT = tmp_root_path
            try:
                _git_revisions.cache_clear()
                revisions = _git_revisions(rel, rev="HEAD")
            finally:
                ROOT = saved_root
                _git_revisions.cache_clear()
            got = [text for _, _, text in revisions]
            want = [None, "v1\n"]
            if got != want:
                ok = False
            print(
                f"  {'ok  ' if got == want else 'FAIL'} want={want!s:<24} got={got!s:<24}  _git_revisions: file-to-symlink transition"
            )

    # _git_revisions: the default rev reads origin's `main`, not ROOT's checked-out branch (ptr727/ProjectTemplate#1017).
    # `origin` is a plain local path here, so the fetch stays offline.
    with tempfile.TemporaryDirectory() as tmp_upstream, tempfile.TemporaryDirectory() as tmp_root:
        tmp_upstream_path = pathlib.Path(tmp_upstream)
        tmp_root_path = pathlib.Path(tmp_root)
        rel = "hub-probe.txt"
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "test@test.invalid"],
            ["git", "config", "user.name", "test"],
        ):
            subprocess.run(cmd, cwd=tmp_upstream_path, check=True, capture_output=True)
        (tmp_upstream_path / rel).write_text("main-content\n")
        subprocess.run(["git", "add", rel], cwd=tmp_upstream_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add on main"],
            cwd=tmp_upstream_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", "-q", str(tmp_upstream_path), str(tmp_root_path)],
            check=True,
            capture_output=True,
        )
        # A clone carries no committer identity of its own (no global config on a CI runner either), so this repo needs the same setup as tmp_upstream_path above.
        for cmd in (
            ["git", "config", "user.email", "test@test.invalid"],
            ["git", "config", "user.name", "test"],
        ):
            subprocess.run(cmd, cwd=tmp_root_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "checkout", "-q", "-b", "develop"],
            cwd=tmp_root_path,
            check=True,
            capture_output=True,
        )
        (tmp_root_path / rel).write_text("develop-only-content\n")
        subprocess.run(["git", "add", rel], cwd=tmp_root_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "develop-only change"],
            cwd=tmp_root_path,
            check=True,
            capture_output=True,
        )
        saved_root = ROOT
        ROOT = tmp_root_path
        try:
            _git_revisions.cache_clear()
            _hub_main_rev.cache_clear()
            revisions = _git_revisions(rel)
        finally:
            ROOT = saved_root
            _git_revisions.cache_clear()
            _hub_main_rev.cache_clear()
    got = [text for _, _, text in revisions]
    want = ["main-content\n"]
    if got != want:
        ok = False
    print(
        f"  {'ok  ' if got == want else 'FAIL'} want={want!s:<24} got={got!s:<24}  _git_revisions: default rev reads origin's main, not the checked-out branch"
    )

    # hub_tracked(): a tracked filename with a byte the active locale rejects must round-trip rather than crash.
    # git ls-tree -z is NUL-delimited raw bytes, and decoding it as text before the NUL-split (the bug review caught) raises UnicodeDecodeError instead of enumerating the path.
    encoding = locale.getpreferredencoding(False)
    invalid_bytes = None
    for candidate in (b"\xff", b"\x80\x81", b"\xfe\xff"):
        try:
            candidate.decode(encoding)
        except UnicodeDecodeError:
            invalid_bytes = candidate
            break
    if invalid_bytes is None:
        # No candidate is actually invalid under this host's active encoding: skip rather than asserting a regression the fixture cannot exercise here.
        print(f"  skip hub_tracked: no candidate byte sequence is invalid under {encoding!r}")
    else:
        bad_name = "bad-" + invalid_bytes.decode("utf-8", errors="surrogateescape") + "-name.txt"
        with tempfile.TemporaryDirectory() as tmp_root:
            tmp_root_path = pathlib.Path(tmp_root)
            for cmd in (
                ["git", "init", "-q", "-b", "main"],
                ["git", "config", "user.email", "test@test.invalid"],
                ["git", "config", "user.name", "test"],
            ):
                subprocess.run(cmd, cwd=tmp_root_path, check=True, capture_output=True)
            try:
                (tmp_root_path / bad_name).write_text("x")
            except OSError:
                # This host's filesystem cannot represent the byte: skip rather than aborting the whole --selftest run over an environment limitation, not a code fault.
                print("  skip hub_tracked: non-UTF-8 filename (host cannot create it)")
            else:
                subprocess.run(
                    ["git", "add", "-A"], cwd=tmp_root_path, check=True, capture_output=True
                )
                subprocess.run(
                    ["git", "commit", "-q", "-m", "add invalid-utf8 name"],
                    cwd=tmp_root_path,
                    check=True,
                    capture_output=True,
                )
                saved_root = ROOT
                ROOT = tmp_root_path
                try:
                    hub_tracked.cache_clear()
                    tracked = hub_tracked(rev="HEAD")
                finally:
                    ROOT = saved_root
                    hub_tracked.cache_clear()
                got = bad_name in tracked
                if not got:
                    ok = False
                print(
                    f"  {'ok  ' if got else 'FAIL'} want=True                     got={got!s:<24}  hub_tracked: a non-UTF-8 filename round-trips instead of crashing"
                )

    # Region extraction and hashing: a forked github-release block must hash differently from the canonical.
    region = split_jobs(rel_ok).get("github-release")
    forked_region = split_jobs(
        rel_ok.replace("          merge-multiple: true\n", "          artifact-ids: 1\n")
    ).get("github-release")
    if (
        region is None
        or forked_region is None
        or content_hash(region) == content_hash(forked_region)
    ):
        ok = False
        print("  FAIL verbatim: forked github-release region should hash differently")
    else:
        print(
            "  ok   verbatim: a forked github-release region hashes differently from the canonical"
        )
    # Section-region extraction, where the region includes the heading line, keeps a nested ### and a fenced ## inside the body, ends at the next sibling H2, is None where absent, and rehashes where the heading is re-cased.
    # The per-section verbatim check depends on every one of these.
    md = "# Title\n\n## Alpha\n\nbody a\n\n```\n## not a heading\n```\n\n### nested\nstill alpha\n\n## Beta\n\nbody b\n"
    a, b, gone = (
        extract_section(md, "Alpha"),
        extract_section(md, "Beta"),
        extract_section(md, "Gamma"),
    )
    spaced = extract_section(
        "##   Alpha\n\nbody a\n", "Alpha"
    )  # extra marker-gap whitespace still locates
    if (
        a is None
        or not a.startswith("## Alpha")
        or "body a" not in a
        or "## not a heading" not in a
        or "still alpha" not in a
        or "body b" in a
        or b is None
        or not b.startswith("## Beta")
        or "body b" not in b
        or "body a" in b
        or gone is not None
        or spaced is None
        or not spaced.startswith("##   Alpha")
        or content_hash(a)
        == content_hash(extract_section(md.replace("## Alpha", "## alpha"), "Alpha"))
    ):
        ok = False
        print("  FAIL section: extract_section region/hash behavior")
    else:
        print(
            "  ok   section: heading in region, fenced ## kept, sibling H2 ends, None if absent, whitespace-tolerant locate, re-cased heading rehashes"
        )
    # A mismatched marker nested inside a fence (a ~~~ line inside a ``` block) must not end the fence early.
    # _fence_step carries this contract, and extract_section and strip_sections each consume it.
    nested = "## Alpha\n```md\n~~~\n## Fake\n```\nreal content\n"
    nested_extract = extract_section(nested, "Alpha")
    nested_strip = strip_sections(
        "## Keep\ntext\n```md\n~~~\n## Fake Drop\n```\nmore text\n## Drop\nreal drop content\n",
        ["Drop"],
    )
    if (
        nested_extract is None
        or "## Fake" not in nested_extract
        or "real content" not in nested_extract
        or "## Fake Drop" not in nested_strip
        or "real drop content" in nested_strip
        or "more text" not in nested_strip
    ):
        ok = False
        print("  FAIL section: a mismatched marker nested in a fence ends the region early")
    else:
        print("  ok   section: a mismatched marker nested in a fence is not a boundary")

    # Coordination-reference scan: the hub name inside a verbatim section is exempt, outside one is not.
    # The first case is the real AGENTS.md shape, where the byte-locked Fleet Bootstrap block must name the hub and a repo therefore cannot clear a finding against it.
    # The CRLF case matters because extract_section normalizes EOLs while a carried file can still arrive CRLF (an operational repo's Windows-native override, or a stale copy mid-resync), so excision must survive that.
    boot = "## Fleet Bootstrap\n\nThe canonical rules live in `github.com/acme/Hub`.\n"
    owned = "## Where the Rules Live\n\nReport a rule discrepancy to acme/Hub.\n"
    clean_doc = (
        "# AGENTS\n\n"
        + boot
        + "\n## Where the Rules Live\n\nState the behavior, not the destination.\n"
    )
    dirty_doc = "# AGENTS\n\n" + boot + "\n" + owned
    tref = [
        ("hub name only inside the verbatim section", clean_doc, {"Fleet Bootstrap"}, False),
        ("hub name in prose the repo owns", dirty_doc, {"Fleet Bootstrap"}, True),
        ("same document with nothing declared verbatim still flags", clean_doc, set(), True),
        (
            "CRLF document excises the same way",
            clean_doc.replace("\n", "\r\n"),
            {"Fleet Bootstrap"},
            False,
        ),
        (
            "a re-cased verbatim heading still excises",
            clean_doc.replace("## Fleet Bootstrap", "## fleet bootstrap"),
            {"Fleet Bootstrap"},
            False,
        ),
        (
            "no hub reference at all",
            "# AGENTS\n\n## Where the Rules Live\n\nNothing to see.\n",
            {"Fleet Bootstrap"},
            False,
        ),
        # A second region under the same heading is excised by name, which is right: both are that section.
        (
            "the heading appearing twice excises both",
            "# AGENTS\n\n" + boot + "\n## Notes\n\n" + boot,
            {"Fleet Bootstrap"},
            False,
        ),
        # A fenced copy is not a heading, so it is prose the repo owns and the reference in it must flag.
        # This is the case that made positional excision necessary, because removing the extracted text instead would delete the fenced copy along with the real region and the scan would fail open.
        # That is the one arrangement a repo could otherwise use to carry the reference in a document it owns.
        (
            "a fenced copy of the section is prose, not the section",
            "# AGENTS\n\n## Notes\n\n```\n" + boot + "```\n\n" + boot,
            {"Fleet Bootstrap"},
            True,
        ),
    ]
    tref_ok = True
    for label, doc, verb, want in tref:
        got = template_ref_outside_verbatim(doc, verb, "acme/Hub")
        if got != want:
            ok = tref_ok = False
            print(f"  FAIL template-ref: {label} (expected {want}, got {got})")
    if tref_ok:
        print(
            f"  ok   template-ref: {len(tref)} cases, verbatim regions excised before the hub-name scan"
        )

    # An H2 the manifest does not declare, in AGENTS.md, GOVERNANCE.md, or .github/copilot-instructions.md.
    # Fence-aware, so a heading syntax example or a shell comment inside a code sample is not misread as a real section.
    uh = [
        (
            "a declared H2 is not flagged",
            "# AGENTS\n\n## Fleet Bootstrap\n\nText.\n",
            {"fleet bootstrap"},
            [],
        ),
        (
            "an undeclared H2 is flagged",
            "# AGENTS\n\n## Fleet Bootstrap\n\nText.\n\n## Local Notes\n\nRepo-specific.\n",
            {"fleet bootstrap"},
            ["local notes"],
        ),
        (
            "declared-name match is case-insensitive",
            "# AGENTS\n\n## fleet BOOTSTRAP\n\nText.\n",
            {"fleet bootstrap"},
            [],
        ),
        (
            "an un-normalized declared set (mixed case, untrimmed) is normalized here, not trusted",
            "# AGENTS\n\n## Fleet Bootstrap\n\nText.\n",
            {" Fleet Bootstrap "},
            [],
        ),
        (
            "an H1 title and a nested H3 are not judged, only H2",
            "# Local Notes\n\n## Fleet Bootstrap\n\n### Local Notes\n\nText.\n",
            {"fleet bootstrap"},
            [],
        ),
        (
            "a fenced sample showing heading syntax is not a real heading",
            "# AGENTS\n\n## Fleet Bootstrap\n\n```\n## Local Notes\n```\n",
            {"fleet bootstrap"},
            [],
        ),
        (
            "a backtick in a backtick-fence info string is not a valid opener, so the heading after it is real",
            "# AGENTS\n\n## Fleet Bootstrap\n\n```md`\n## Local Notes\n",
            {"fleet bootstrap"},
            ["local notes"],
        ),
        (
            "a repo's own local content, undeclared, is flagged even in a file with its own declared sections",
            (
                "# Copilot Instructions\n\n## GitHub Copilot Review Runbook\n\nText.\n\n"
                "## Development Workflow\n\nLocal build and test steps.\n\n"
                "## Command Line Usage\n\nLocal CLI reference.\n"
            ),
            {"github copilot review runbook"},
            ["command line usage", "development workflow"],
        ),
    ]
    uh_ok = True
    for label, doc, declared, want in uh:
        got = undeclared_h2_headings(doc, declared)
        if got != want:
            ok = uh_ok = False
            print(f"  FAIL undeclared-heading: {label} (expected {want}, got {got})")
    if uh_ok:
        print(f"  ok   undeclared-heading: {len(uh)} cases, H2-only, case-insensitive, fence-aware")

    # A fence closes only on a same-family marker at least as long as the opener.
    uf = [
        ("a simple ``` fence excludes its content", "```\n## Phantom\n```\n## Real\n", "## Real\n"),
        ("a simple ~~~ fence excludes its content", "~~~\n## Phantom\n~~~\n## Real\n", "## Real\n"),
        (
            "a ~~~ line nested inside a ``` fence does not close it",
            "```md\n~~~\n## Phantom\n```\n## Real\n",
            "## Real\n",
        ),
        (
            "a shorter ``` cannot close a longer ```` fence",
            "````md\n## Phantom\n```\n## Real\n````\n",
            "",
        ),
        (
            "a longer closing fence than the opener still closes",
            "```\n## Phantom\n````\n## Real\n",
            "## Real\n",
        ),
        (
            "a marker run followed by trailing text does not close",
            "```\n## Phantom\n```not-a-fence\n## Real\n```\n## Real2\n",
            "## Real2\n",
        ),
        (
            "trailing whitespace after the marker run still closes",
            "```\n## Phantom\n```   \n## Real\n",
            "## Real\n",
        ),
        (
            "a 3-space-indented closing fence still closes",
            "```\n## Phantom\n   ```\n## Real\n",
            "## Real\n",
        ),
        (
            "a 4-space-indented closing fence remains content, not a boundary",
            "```\n## Phantom\n    ```\n## Still Phantom\n```\n## Real\n",
            "## Real\n",
        ),
        (
            "a 4-space-indented opener never opens a fence",
            "    ```\n## Real\n",
            "    ```\n## Real\n",
        ),
        (
            "a backtick in a backtick-fence info string is not a valid opener",
            "```md`\n## Real\n",
            "```md`\n## Real\n",
        ),
        (
            "a backtick in a tilde-fence info string does not disqualify it",
            "~~~md`\n## Phantom\n~~~\n## Real\n",
            "## Real\n",
        ),
    ]
    uf_ok = True
    for label, doc, want in uf:
        got = unfenced_text(doc)
        if got != want:
            ok = uf_ok = False
            print(f"  FAIL unfenced-text: {label} (expected {want!r}, got {got!r})")
    if uf_ok:
        print(
            f"  ok   unfenced-text: {len(uf)} cases, closing fence matches the opener's marker and length"
        )

    # Issue generator: findings land in the right buckets and the title carries the count.
    fe = {"name": "Widget", "types": ["python"]}
    it, ib = render_issue(
        fe,
        [("LETTER", "file: X absent"), ("DRIFT", "verbatim: Y differs"), ("ERROR", "gh failed")],
        "main",
        "abc1234",
        "2026-01-01T00:00:00Z",
        "hub123",
    )
    et, eb = render_issue(fe, [], "main", "abc1234", "2026-01-01T00:00:00Z", "hub123")
    if (
        "Widget" not in it
        or "(3 findings)" not in it
        or "## Must fix" not in ib
        or "**LETTER** file: X absent" not in ib
        or "## Converge" not in ib
        or "verbatim: Y differs" not in ib
        or "## Could not verify" not in ib
        or "(0 findings)" not in et
        or "nothing to converge" not in eb
    ):
        ok = False
        print("  FAIL issue: render_issue grouping/title/empty behavior")
    else:
        print(
            "  ok   issue: render_issue groups must-fix/converge/unverifiable, counts findings, handles the clean case"
        )

    # README/HISTORY mirror: same title+intro matches modulo the ToC-omit comment, and intro drift is caught.
    r_md = "# Widget <!-- omit from toc -->\n\nDoes widget things.\n\n## Build\n"
    h_md = "# Widget\n\nDoes widget things.\n\n## Release History\n"
    h_bad = "# Widget\n\nDoes other things.\n\n## Release History\n"
    r_two = "# W\n\nLine one.\n\nLine two.\n\n## Build\n"
    h_joined = "# W\n\nLine one.\nLine two.\n\n## Release History\n"
    if (
        title_and_intro(r_md) != title_and_intro(h_md)
        or title_and_intro(r_md) == title_and_intro(h_bad)
        or title_and_intro(r_two) == title_and_intro(h_joined)
    ):
        ok = False
        print("  FAIL history: README/HISTORY title+intro mirror detection")
    else:
        print(
            "  ok   history: mirror matches modulo the ToC-omit comment, intro drift and paragraph-boundary drift detected"
        )
    # Description mirror: links reduce to their text, and a link-free line passes through unchanged.
    linked = "Utility to clean [media](https://x.example/Foo_(bar)) per the [spec][spec-ref]."
    nested = "See [API [docs]](https://example.test/a_(b)_(c)) for details."
    # A failed outer span used to jump past the whole run instead of retrying one character in,
    # so a link nested inside a non-link bracket run was skipped (#1011, qodo).
    inner_link = "See [[docs](url)] for details."
    # A backslash-escaped `\[` used to count as real nesting, corrupting the label match instead
    # of being read as a literal character (#1011, qodo).
    escaped_bracket = r"See [API \[docs](url) for details."
    if (
        strip_md_links(linked) != "Utility to clean media per the spec."
        or strip_md_links("Plain intro line.") != "Plain intro line."
        or strip_md_links(nested) != "See API [docs] for details."
        or strip_md_links(inner_link) != "See [docs] for details."
        or strip_md_links(escaped_bracket) != r"See API \[docs for details."
    ):
        ok = False
        print("  FAIL description: strip_md_links behavior")
    else:
        print(
            "  ok   description: Markdown links reduce to their text, plain text passes through, nested brackets/parens balance, nested and escaped labels handled"
        )
    # A run of unmatched '[' used to re-scan the remaining text from every position (#1011, CodeRabbit),
    # O(N^2) on a README tagline read before any length limit. Linear now: a slow run means a regression.
    pathological = "[" * 20000
    start = time.monotonic()
    strip_md_links(pathological)
    elapsed = time.monotonic() - start
    if elapsed > 1.0:
        ok = False
        print(
            f"  FAIL description: strip_md_links took {elapsed:.2f}s on unmatched brackets, expected linear"
        )
    else:
        print(
            f"  ok   description: strip_md_links stays linear on unmatched brackets ({elapsed:.3f}s for 20000)"
        )
    # cspell duplication: a workspace cSpell word list is detected, and a mere cspell.json mention is not.
    ws_dup = '{ "settings": { "cSpell.words": ["foo"] } }'
    ws_ok = '{ "settings": { "editor.rulers": [100] }, "note": "words live in cspell.json" }'
    if not workspace_cspell_words(ws_dup) or workspace_cspell_words(ws_ok):
        ok = False
        print("  FAIL cspell: workspace word-list detection")
    else:
        print(
            "  ok   cspell: workspace cSpell word list detected, a plain cspell.json mention is not"
        )

    # The branch-drift direction split, covering a modify, add and delete on main plus a develop-only change.
    bd_base = {"keep": "a", "moda": "1", "modb": "2", "deld": "e", "devonly": "x"}
    bd_main = {
        "keep": "a",
        "moda": "9",
        "modb": "9",
        "add": "n",
        "devonly": "x",
    }  # moved moda/modb, added 'add', deleted 'deld'
    bd_dev = {
        "keep": "a",
        "moda": "1",
        "modb": "7",
        "add": "m",
        "deld": "e",
        "devonly": "y",
    }  # still at base on moda/deld, moved modb/add/devonly
    bd_behind, bd_diverged = classify_branch_drift(bd_base, bd_main, bd_dev)
    if bd_behind != ["deld", "moda"] or bd_diverged != ["add", "modb"]:
        ok = False
        print(f"  FAIL branch-drift classify -> behind={bd_behind} diverged={bd_diverged}")
    else:
        print(
            "  ok   branch-drift: behind (modify/delete develop still at base) vs diverged (both moved), develop-only excluded"
        )

    # CLI parsing, where a repo name and a flag value must not be confused for one another.
    # The previous hand-rolled parse took every non `--` argument as a repo name, so `--branch develop` would have audited a repo called "develop" rather than overriding the branch.
    cli_cases = [
        ([], [], None, False, False),
        (["Utilities"], ["Utilities"], None, False, False),
        (["Utilities", "--branch", "develop"], ["Utilities"], "develop", False, False),
        (["--branch=feature/x", "Utilities"], ["Utilities"], "feature/x", False, False),
        (["--issue", "Utilities"], ["Utilities"], None, True, False),
        (["--selftest"], [], None, False, True),
    ]
    for argv, names, branch, issue, selftest in cli_cases:
        got = parse_args(argv)
        if (got.names, got.branch, got.issue, got.selftest) != (names, branch, issue, selftest):
            ok = False
            print(f"  FAIL cli parse {argv} -> {got}")
        else:
            print(f"  ok   cli: {argv or ['(no args)']} -> names={names} branch={branch}")

    # The override wins over the registry field, and the registry default is main.
    ground_cases = [
        ({}, None, "main"),
        ({"groundTruthBranch": "develop"}, None, "develop"),
        ({"groundTruthBranch": "main"}, "develop", "develop"),
        ({}, "feature/x", "feature/x"),
    ]
    for entry, branch, want in ground_cases:
        got = ground_branch_of(entry, branch)
        if got != want:
            ok = False
            print(f"  FAIL ground branch {entry} + {branch} -> {got}, want {want}")
        else:
            print(
                f"  ok   ground branch: registry={entry.get('groundTruthBranch')} override={branch} -> {want}"
            )

    # The driftNote freshness rules, driven directly rather than through audit_repo.
    # That exercises both the clean and the unclean audit without standing up a whole conformant repo to reach one branch.
    note_spec = {
        "types": {
            "types": {
                "hugo": {"checks": [{"id": "hugo.generator.pinned"}]},
                "docker": {"checks": [{"id": "docker.cache.registry"}]},
            },
            "crossCutting": {"setup": {"checks": [{"id": "setup.driftnotes.current"}]}},
        }
    }
    note_cases = [
        # The end-of-sentence period is the case an end-anchored pattern misses, and it is how both notes this form was introduced for are written.
        (
            "check id followed by a period",
            ["hugo"],
            ["Pinned in two workflows (hugo.generator.pinned)."],
            0,
            ["does not evaluate"],
        ),
        (
            "check id mid-sentence",
            ["hugo"],
            ["The pin (hugo.generator.pinned) is duplicated."],
            0,
            ["does not evaluate"],
        ),
        (
            "cross-cutting id, which no repo declares",
            ["hugo"],
            ["Notes go stale (setup.driftnotes.current)."],
            0,
            ["does not evaluate"],
        ),
        (
            "check id of an undeclared type",
            ["hugo"],
            ["Cache layer (docker.cache.registry)."],
            0,
            ["this repo does not declare"],
        ),
        (
            "check id absent from the catalog",
            ["hugo"],
            ["Theme record (hugo.vendored.provenence)."],
            0,
            ["does not define"],
        ),
        # A version string in parentheses is the false positive the id shape has to exclude.
        (
            "parenthesized version, not a check id",
            ["hugo"],
            ["Generator held at (0.164.0) by the composite action."],
            0,
            [],
        ),
        (
            "permanent deviation, no marker and no id",
            ["hugo"],
            ["Relies on validate-task, having no get-version-task."],
            0,
            [],
        ),
        (
            "marker note on a clean audit",
            ["hugo"],
            ["The sibling doc is pending fleet ratification."],
            0,
            ["but the audit is clean"],
        ),
        # The case the old gate suppressed outright: one unclearable finding, and the note never checked.
        (
            "marker note with findings open",
            ["hugo"],
            ["The sibling doc is pending fleet ratification."],
            1,
            ["while 1 finding(s) are open"],
        ),
    ]
    for label, types, notes, open_count, wanted in note_cases:
        got = driftnote_findings({"types": types, "driftNotes": notes}, note_spec, open_count)
        texts = [t for _, t in got]
        if len(texts) != len(wanted) or not all(w in t for w, t in zip(wanted, texts)):
            ok = False
            print(f"  FAIL driftNote {label} -> {texts}, want {wanted}")
        else:
            print(f"  ok   driftNote {label}: {len(texts)} finding(s)")

    # A spec with no catalog and an entry with a check-id note fails loudly and names what is missing.
    # No live caller pairs the two, since main() always loads the catalog.
    # The pairing stays an error rather than becoming a fallback, because resolving ids against an absent catalog reports every one of them undefined.
    # That is a work list which destroys correct notes.
    try:
        driftnote_findings(
            {"types": ["hugo"], "driftNotes": ["A note (hugo.build.strict)."]}, {"registry": {}}, 0
        )
        ok = False
        print("  FAIL missing catalog: no error raised")
    except KeyError as e:
        if "project-types.json" not in str(e):
            ok = False
            print(f"  FAIL missing catalog: error does not name the file -> {e}")
        else:
            print("  ok   missing catalog: raises and names spec/project-types.json")

    # The README structure engine, run against the real declared model rather than a fixture of it.
    # A model edit that contradicts these checks then fails here instead of on the fleet.
    rm = load("spec/readme-sections.json")
    conformant = (
        "# Fixture\n\nA fixture repository.\n\n## Build and Distribution\n\n- **Source Code**: [GitHub][gh]\n\n"
        "### Build Status\n\n[![Release Status][a]][x]\\\n[![Last Commit][b]][x]\n\n"
        "### Releases\n\n[![GitHub Release][c]][x]\\\n[![GitHub Pre-Release][d]][x]\n\n"
        "### Release Notes\n\n**Version**: 1.0\n\n## Table of Contents\n\n- [Overview](#overview)\n\n"
        "## Overview\n\nWhat it does.\n\n## Whatever This Repo Calls It\n\nRepo-specific.\n\n"
        "## Questions or Issues\n\nOpen an issue.\n\n## 3rd Party Tools\n\n- [Thing][t]\n\n"
        "## License\n\nLicensed under the [MIT License][license]\\\n![License][license-shield]\n\n"
        "<!-- Shields -->\n\n"
        "[a]: https://img.shields.io/github/actions/workflow/status/o/r/publish-release.yml?label=Releases%20Build\n"
        "[b]: https://img.shields.io/github/last-commit/o/r\n"
        "[c]: https://img.shields.io/github/v/release/o/r?label=GitHub%20Release\n"
        "[d]: https://img.shields.io/github/v/release/o/r?include_prereleases&label=GitHub%20Pre-Release\n"
        "[license-shield]: https://img.shields.io/github/license/o/r\n"
    )
    # The four repos that suffix every heading for the ToC extension must read identically to the plain form.
    omit_toc = re.sub(
        r"^(#{2,3} .*)$", r"\1 <!-- omit from toc -->", conformant, flags=re.MULTILINE
    )
    readme_cases = [
        ("a conformant README, repo-specific section included", conformant, set(), True, 0),
        (
            "a second intro paragraph is not a finding",
            conformant.replace(
                "A fixture repository.\n",
                "A fixture repository.\n\nAnd a clarifying paragraph about it.\n",
            ),
            set(),
            True,
            0,
        ),
        (
            "a retired section name reports a rename",
            conformant.replace("## Overview", "## Features"),
            set(),
            True,
            1,
        ),
        (
            "Questions or Issues is optional while private",
            conformant.replace("## Questions or Issues\n\nOpen an issue.\n\n", ""),
            set(),
            False,
            0,
        ),
        (
            "Questions or Issues is required when public",
            conformant.replace("## Questions or Issues\n\nOpen an issue.\n\n", ""),
            set(),
            True,
            1,
        ),
        (
            "3rd Party Tools is required in every repo",
            conformant.replace("## 3rd Party Tools\n\n- [Thing][t]\n\n", ""),
            set(),
            True,
            1,
        ),
        (
            "a Table of Contents is required with no size threshold",
            conformant.replace("## Table of Contents\n\n- [Overview](#overview)\n\n", ""),
            set(),
            True,
            1,
        ),
        (
            "an out-of-order declared section reports once",
            conformant.replace("## Questions or Issues\n\nOpen an issue.\n\n", "").replace(
                "## Overview", "## Questions or Issues\n\nOpen an issue.\n\n## Overview"
            ),
            set(),
            True,
            1,
        ),
        (
            "License must be the last section",
            conformant + "\n## TODO\n\nA backlog.\n",
            set(),
            True,
            1,
        ),
        (
            "a missing Release Notes sub-section reports once",
            conformant.replace("### Release Notes\n\n**Version**: 1.0\n\n", ""),
            set(),
            True,
            1,
        ),
        (
            "a `## ` line inside a fence is not a section",
            conformant.replace("What it does.", "```md\n## Not A Section\n```"),
            set(),
            True,
            0,
        ),
        ("Usage and Installation are N/A for source-only", conformant, {"source-only"}, True, 0),
        ("a ToC-omit comment on every heading changes nothing", omit_toc, set(), True, 0),
    ]
    for label, text, sel_types, public, wantn in readme_cases:
        got = readme_section_findings(text, rm, sel_types, public)
        if len(got) != wantn:
            ok = False
        print(
            f"  {'ok  ' if len(got) == wantn else 'FAIL'} want={wantn} got={len(got)}  readme sections: {label}"
        )
        if len(got) != wantn:
            for _, t in got:
                print(f"         {t}")

    docker_shield = "[![Docker Latest][e]][x]\n[e]: https://img.shields.io/docker/v/o/r/latest\n"
    inline_all = re.sub(
        r"!\[([^\]]*)\]\[([a-z-]+)\]",
        lambda m: f"![{m.group(1)}](https://img.shields.io/PLACEHOLDER-{m.group(2)})",
        conformant,
    )
    shield_cases = [
        ("base shields only, a repo publishing nothing", conformant, {}, 0),
        ("a ToC-omit comment on every heading changes nothing", omit_toc, {}, 0),
        ("a docker repo owes a version shield", conformant, {"publish": [{"target": "docker"}]}, 1),
        (
            "a docker repo carrying one is satisfied, whatever its channel",
            conformant.replace(
                "[![GitHub Pre-Release][d]][x]\n",
                "[![GitHub Pre-Release][d]][x]\\\n" + docker_shield,
            ),
            {"publish": [{"target": "docker"}]},
            0,
        ),
        (
            "a nuget repo owes one version shield, no prerelease",
            conformant,
            {"publish": [{"target": "nuget"}]},
            1,
        ),
        (
            "a pypi repo owes one version shield, no prerelease",
            conformant,
            {"publish": [{"target": "pypi"}]},
            1,
        ),
        (
            "a caption the repo spells differently is not a finding",
            conformant.replace("![Release Status]", "![Lint Build]"),
            {},
            0,
        ),
        (
            "an extra shield beyond the class is not a finding",
            conformant.replace(
                "[![Last Commit][b]][x]", "[![Last Commit][b]][x]\\\n[![Last Build][a]][x]"
            ),
            {},
            0,
        ),
        (
            "a shield in the wrong sub-section does not count as present",
            conformant.replace("[![GitHub Release][c]][x]\\\n", ""),
            {},
            1,
        ),
        # A badge shown as a code sample is markup, so it satisfies nothing and trips nothing.
        # Every region the README checks read comes through readme_region, which strips fences at the root.
        (
            "a fenced badge sample does not satisfy a required shield",
            conformant.replace(
                "[![GitHub Release][c]][x]\\\n", "```md\n[![GitHub Release][c]][x]\n```\n"
            ),
            {},
            1,
        ),
        (
            "a fenced license shield does not trip the exclusive rule",
            conformant.replace(
                "## Overview", "```md\n![License][license-shield]\n```\n\n## Overview"
            ),
            {},
            0,
        ),
        (
            "a retired badge service is reported wherever it sits",
            conformant.replace(
                "[license-shield]: https://img.shields.io/github/license/o/r\n",
                "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
            ),
            {},
            1,
        ),
        # The case above named every placement and read only the reference block, so an inline badge was invisible rather than wrong.
        # That is the reading shield_endpoints already takes for every other shield, and this one had not taken it.
        (
            "a retired badge written inline is reported",
            conformant.replace(
                "[![Last Commit][b]][x]",
                "[![Last Commit][b]][x]\\\n![Last Build](https://byob.yarr.is/o/r/lastbuild)",
            ),
            {},
            1,
        ),
        (
            "a retired badge defined and rendered is one finding, not two",
            conformant.replace(
                "[![Last Commit][b]][x]",
                "[![Last Commit][b]][x]\\\n![Last Build][last-build-shield]",
            ).replace(
                "[license-shield]: https://img.shields.io/github/license/o/r\n",
                "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
            ),
            {},
            1,
        ),
        (
            "a retired badge shown as a fenced sample is markup",
            conformant.replace(
                "## Overview",
                "```md\n![Last Build](https://byob.yarr.is/o/r/lastbuild)\n```\n\n## Overview",
            ),
            {},
            0,
        ),
        # The wording follows which of the three shapes it is, since a definition nothing renders is not rendering anything.
        # Saying it renders sends the reader looking for a badge that is not on the page.
        (
            "an unrendered definition says so rather than claiming a render",
            conformant.replace(
                "[license-shield]: https://img.shields.io/github/license/o/r\n",
                "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
            ),
            {},
            1,
            "nothing renders it",
        ),
        (
            "a rendered definition says renders",
            conformant.replace(
                "[![Last Commit][b]][x]",
                "[![Last Commit][b]][x]\\\n![Last Build][last-build-shield]",
            ).replace(
                "[license-shield]: https://img.shields.io/github/license/o/r\n",
                "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
            ),
            {},
            1,
            "renders the byob",
        ),
        # The same endpoint rendered inline leaves this definition unused, so attributing by URL would credit the render to a reference nothing uses.
        (
            "an unused definition beside an inline render is not credited with it",
            conformant.replace(
                "[![Last Commit][b]][x]",
                "[![Last Commit][b]][x]\\\n![Last Build](https://byob.yarr.is/o/r/lastbuild)",
            ).replace(
                "[license-shield]: https://img.shields.io/github/license/o/r\n",
                "[license-shield]: https://img.shields.io/github/license/o/r\n[last-build-shield]: https://byob.yarr.is/o/r/lastbuild\n",
            ),
            {},
            1,
            "rendered elsewhere",
        ),
        (
            "the pre-release shield is told from the release shield by its query",
            conformant.replace(
                "?include_prereleases&label=GitHub%20Pre-Release", "?label=Another%20Release"
            ),
            {},
            1,
        ),
        # The license shield is an ordinary member of the base class, addressed to a different section.
        ("the license shield in the closing License section", conformant, {}, 0),
        (
            "no license shield at all",
            conformant.replace("\\\n![License][license-shield]", ""),
            {},
            1,
        ),
        (
            "the license shield left under Releases",
            conformant.replace("\\\n![License][license-shield]", "").replace(
                "[![GitHub Pre-Release][d]][x]",
                "[![GitHub Pre-Release][d]][x]\\\n![License][license-shield]",
            ),
            {},
            2,
        ),
        (
            "a shield's own reference definition is not a use of it",
            conformant.replace("![License][license-shield]\n", ""),
            {},
            1,
        ),
        # An inline shield is resolved rather than silently invisible.
        # Reading references alone let a repo writing every badge inline pass by carrying nothing the check could see.
        (
            "inline shields count as present",
            inline_all.replace("PLACEHOLDER-a", "github/actions/workflow/status/o/r")
            .replace("PLACEHOLDER-b", "github/last-commit/o/r")
            .replace("PLACEHOLDER-c", "github/v/release/o/r")
            .replace("PLACEHOLDER-d", "github/v/release/o/r?include_prereleases")
            .replace("PLACEHOLDER-license-shield", "github/license/o/r"),
            {},
            0,
        ),
        (
            "an inline shield in the wrong section is still exclusive",
            conformant.replace(
                "## Overview",
                "![License](https://img.shields.io/github/license/o/r)\n\n## Overview",
            ),
            {},
            1,
        ),
    ]
    # A case may carry a fifth element, a substring the finding text must contain.
    # A count alone cannot tell one wording from another, and the wording is the whole subject of some of these cases.
    for case in shield_cases:
        label, text, ent, wantn = case[:4]
        want_text = case[4] if len(case) > 4 else None
        got = readme_shield_findings(text, rm, ent)
        good = len(got) == wantn and (want_text is None or any(want_text in t for _, t in got))
        if not good:
            ok = False
        shown = f"want={wantn}" if want_text is None else f"want={wantn}+'{want_text}'"
        print(f"  {'ok  ' if good else 'FAIL'} {shown} got={len(got)}  readme shields: {label}")
        if not good:
            for _, t in got:
                print(f"         {t}")

    links_ok = (
        "# F\n\nA fixture.\n\n## X\n\n[a](#x) [b][gh] [c][docker-hub-link] [d][agents] [e][upstream-link]\n\n"
        "![License][license-shield]\n\n"
        "<!-- Sections -->\n\n[x-anchor]: #x\n\n<!-- Shields -->\n\n[license-shield]: https://img.shields.io/github/license/o/r\n\n"
        "<!-- Distribution -->\n\n[actions-link]: https://github.com/o/r/actions\n[docker-hub-link]: https://hub.docker.com/r/o/r\n[github-link]: https://github.com/o/r\n\n"
        "<!-- Repo -->\n\n[agents]: ./AGENTS.md\n[license]: ./LICENSE\n\n"
        "<!-- External -->\n\n[upstream-link]: https://github.com/someone-else/their-repo\n"
    )
    # Kept in reference-name order, so the case measures the per-target rule rather than tripping the sort check.
    two_images = links_ok.replace(
        "[github-link]: https://github.com/o/r\n",
        "[github-link]: https://github.com/o/r\n[lsio-docker-hub-link]: https://hub.docker.com/r/o/r-lsio\n",
    )
    swapped = links_ok.replace(
        "<!-- Sections -->\n\n[x-anchor]: #x\n\n<!-- Shields -->\n\n[license-shield]: https://img.shields.io/github/license/o/r\n",
        "<!-- Shields -->\n\n[license-shield]: https://img.shields.io/github/license/o/r\n\n<!-- Sections -->\n\n[x-anchor]: #x\n",
    )
    link_cases = [
        ("a conformant reference block", links_ok, 0, 0),
        (
            "a shield reference not ending -shield",
            links_ok.replace("[license-shield]", "[licence]"),
            1,
            0,
        ),
        (
            "a URI reference not ending -link",
            links_ok.replace("[upstream-link]", "[upstream]"),
            1,
            0,
        ),
        (
            "a repo-local reference ending -link",
            links_ok.replace("[agents]:", "[agents-link]:"),
            1,
            0,
        ),
        (
            "the repo root named for the project",
            links_ok.replace("[github-link]", "[fixture-link]"),
            1,
            0,
        ),
        ("somebody else's GitHub repo is not renamed", links_ok, 0, 0),
        ("one Docker Hub image takes the bare name", links_ok, 0, 0),
        ("two Docker Hub images each take a target prefix", two_images, 1, 0),
        (
            "two Docker Hub images, both prefixed, is clean",
            two_images.replace(
                "[docker-hub-link]: https://hub.docker.com/r/o/r\n",
                "[base-docker-hub-link]: https://hub.docker.com/r/o/r\n",
            ),
            0,
            0,
        ),
        (
            "an undeclared group header",
            links_ok.replace("<!-- External -->", "<!-- Other links -->"),
            0,
            1,
        ),
        ("groups out of the declared order", swapped, 0, 1),
        (
            "a group not sorted by reference name",
            links_ok.replace(
                "[agents]: ./AGENTS.md\n[license]: ./LICENSE\n",
                "[license]: ./LICENSE\n[agents]: ./AGENTS.md\n",
            ),
            0,
            1,
        ),
        (
            "a reference in the wrong group",
            links_ok.replace("[license]: ./LICENSE\n", "").replace(
                "<!-- External -->\n", "<!-- External -->\n\n[license]: ./LICENSE\n"
            ),
            0,
            1,
        ),
        # A shields.io URL the document never renders is judged by that usage rather than by its host, so it is an ordinary URI.
        # Across the fleet every one of the 119 img.shields.io definitions is rendered, so a host test decides nothing.
        (
            "an unrendered shields.io reference is a URI, not a shield",
            links_ok.replace(
                "[license-shield]: https://img.shields.io/github/license/o/r\n",
                "[extra-shield]: https://img.shields.io/badge/never-rendered-blue\n[license-shield]: https://img.shields.io/github/license/o/r\n",
            ),
            1,
            1,
        ),
        # A code sample showing badge markup is markup rather than a badge.
        # Reading a fenced block would invent a group header, two definitions, and a rendered shield the document does not have.
        (
            "a fenced code sample is not read as definitions",
            links_ok
            + "\n## Sample\n\n```md\n<!-- Other links -->\n\n[wrong-name]: https://example.test/\n![License][license-shield]\n```\n",
            0,
            0,
        ),
        # A dependency hosted where this project also publishes stays external, so it keeps its own name.
        # The owner-scoped prefixes are what separate the two, and they live in the model rather than in the code.
        (
            "somebody else's NuGet package is not renamed nuget-link",
            links_ok.replace(
                "[upstream-link]: https://github.com/someone-else/their-repo",
                "[upstream-link]: https://www.nuget.org/packages/Serilog/",
            ),
            0,
            0,
        ),
        (
            "this project's own NuGet package is renamed",
            links_ok.replace(
                "[upstream-link]: https://github.com/someone-else/their-repo",
                "[upstream-link]: https://www.nuget.org/packages/o.Widget/",
            ),
            1,
            1,
        ),
        # A comment that opens no group is not a group header.
        # A markdownlint directive sits on its own line exactly like one, and reading it as a group reports a header the document never declared.
        (
            "a directive comment is not a link group",
            "<!-- markdownlint-disable MD033 -->\n" + links_ok,
            0,
            0,
        ),
        # Also when it sits inside the reference block, where definitions do fall under it.
        (
            "a directive inside the reference block is not a link group",
            links_ok.replace(
                "[agents]: ./AGENTS.md",
                "<!-- markdownlint-disable MD034 -->\n[agents]: ./AGENTS.md",
            ),
            0,
            0,
        ),
        # A scheme makes a target a link rather than a file, so it is named `-link` and grouped External.
        # Reading `mailto:` as a repo path would demand a bare name and the Repo group for a contact address.
        (
            "a mailto target is a URI, not a repo path",
            links_ok.replace(
                "[upstream-link]: https://github.com/someone-else/their-repo",
                "[upstream-link]: mailto:someone@example.test",
            ),
            0,
            0,
        ),
        (
            "a mailto target named as a repo path is reported",
            links_ok.replace(
                "[upstream-link]: https://github.com/someone-else/their-repo",
                "[upstream]: mailto:someone@example.test",
            ),
            1,
            0,
        ),
        # Two names may point at one URL, and only the rendered one is a shield.
        # Keying on the URL instead would make the plain link a shield because its twin is rendered.
        (
            "a second reference to a rendered URL is not itself a shield",
            links_ok.replace(
                "[upstream-link]: https://github.com/someone-else/their-repo",
                "[upstream-link]: https://img.shields.io/github/license/o/r",
            ),
            0,
            0,
        ),
    ]
    for label, text, want_letter, want_drift in link_cases:
        got = readme_link_findings(text, rm, "o/r")
        gl = sum(1 for k, _ in got if k == "LETTER")
        gd = sum(1 for k, _ in got if k == "DRIFT")
        good = (gl, gd) == (want_letter, want_drift)
        if not good:
            ok = False
        print(
            f"  {'ok  ' if good else 'FAIL'} want={want_letter}L/{want_drift}D got={gl}L/{gd}D  readme links: {label}"
        )
        if not good:
            for k, t in got:
                print(f"         {k}: {t}")

    # The shared tool catalog, checked over the intersection only: a repo's own tools are its own business.
    cat = load("spec/third-party-tools.json")
    tools_head = "# F\n\nA fixture.\n\n## 3rd Party Tools\n\n"
    tools_defs = "\n[cspell-link]: https://cspell.org\n[widget-link]: https://widget.example/\n"
    tool_cases = [
        (
            "a catalog tool matching the catalog",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
            + tools_defs,
            0,
        ),
        (
            "a catalog tool described differently",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell-checks README.md in CI. |\n"
            + tools_defs,
            1,
        ),
        (
            "a catalog tool with no description at all",
            tools_head + "- [cspell][cspell-link]\n" + tools_defs,
            1,
        ),
        (
            "a catalog tool linked by another URL",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][widget-link] | Spell checker. |\n"
            + tools_defs,
            1,
        ),
        (
            "a tool the catalog does not name is not judged",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [Widget][widget-link] | whatever this repo calls it |\n"
            + tools_defs,
            0,
        ),
        (
            "the bullet form is read like the table form",
            tools_head + "- [cspell][cspell-link] - Spell checker.\n" + tools_defs,
            0,
        ),
        (
            "no section yields nothing, since its absence is already reported",
            "# F\n\nA fixture.\n\n## Other\n\nx\n",
            0,
        ),
        # A definition inside a code sample is markup being shown, so it must not resolve a tool link.
        # The sample sits after the real definition deliberately, so reading across fences would override it.
        (
            "a fenced sample does not supply a tool link",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
            + tools_defs
            + "\n```md\n[cspell-link]: https://wrong.example/\n```\n",
            0,
        ),
        # A tool table shown as a sample inside the section is markup, not entries.
        (
            "a fenced tool table is not read as entries",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n\n```md\n| [cspell][cspell-link] | wrong description |\n```\n"
            + tools_defs,
            0,
        ),
        # The list is alphabetized, which spec/readme-structure.md states and nothing checked until now.
        (
            "an unsorted tool list is reported",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [markdownlint-cli2][md-link] | Markdown linter. |\n| [cspell][cspell-link] | Spell checker. |\n"
            + tools_defs
            + "[md-link]: https://github.com/DavidAnson/markdownlint-cli2\n",
            1,
        ),
        (
            "a sorted tool list is not",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n| [markdownlint-cli2][md-link] | Markdown linter. |\n"
            + tools_defs
            + "[md-link]: https://github.com/DavidAnson/markdownlint-cli2\n",
            0,
        ),
        # GitHub's Markdown makes both outer pipes optional, so a row written without either is an entry rather than a non-row.
        # Requiring them read such a table as empty, which scores as clean rather than as unread.
        (
            "a row with no trailing pipe is read",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker.\n"
            + tools_defs,
            0,
        ),
        (
            "a row with no trailing pipe is judged like any other",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell-checks the README in CI.\n"
            + tools_defs,
            1,
        ),
        (
            "a whole table with no trailing pipes is ordered",
            tools_head
            + "| Tool | Role\n| --- | ---\n| [markdownlint-cli2][md-link] | Markdown linter.\n| [cspell][cspell-link] | Spell checker.\n"
            + tools_defs
            + "[md-link]: https://github.com/DavidAnson/markdownlint-cli2\n",
            1,
        ),
        (
            "a row with no leading pipe is read",
            tools_head
            + "Tool | Role |\n--- | --- |\n[cspell][cspell-link] | Spell checker. |\n"
            + tools_defs,
            0,
        ),
        (
            "a table with neither outer pipe is judged",
            tools_head
            + "Tool | Role\n--- | ---\n[cspell][cspell-link] | Spell-checks the README in CI.\n"
            + tools_defs,
            1,
        ),
        # The License column is forbidden by spec/readme-structure.md, and project-types.json asserted that as `letter` before anything read it.
        # One finding is raised for the table, read off its header, rather than one per row.
        (
            "a License column is reported once",
            tools_head
            + "| Tool | Role | License |\n| --- | --- | --- |\n| [cspell][cspell-link] | Spell checker. | MIT |\n| [markdownlint-cli2][md-link] | Markdown linter. | MIT |\n"
            + tools_defs
            + "[md-link]: https://github.com/DavidAnson/markdownlint-cli2\n",
            1,
        ),
        (
            "a License column does not stop the rows being read",
            tools_head
            + "| Tool | Role | License |\n| --- | --- | --- |\n| [cspell][cspell-link] | Spell-checks the README in CI. | MIT |\n"
            + tools_defs,
            2,
        ),
        (
            "a table with no License column is not reported",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
            + tools_defs,
            0,
        ),
        # A tool whose own role is the word license must not read as the column, since the header is a header.
        (
            "a row cell reading License is not the column",
            tools_head
            + "| Tool | Role |\n| --- | --- |\n| [Widget][widget-link] | License |\n"
            + tools_defs,
            0,
        ),
        (
            "a License column with no outer pipes is reported",
            tools_head
            + "Tool | Role | License\n--- | --- | ---\n[cspell][cspell-link] | Spell checker. | MIT\n"
            + tools_defs,
            1,
        ),
        # A thematic break carries no pipe, so it is not a one-cell delimiter row and the paragraph above it is not a header.
        (
            "a thematic break is not a delimiter row",
            tools_head
            + "Licensing notes follow.\n\n---\n\n| Tool | Role |\n| --- | --- |\n| [cspell][cspell-link] | Spell checker. |\n"
            + tools_defs,
            0,
        ),
        # Prose mentioning a tool is not a row, since a link alone does not make one.
        (
            "prose naming a cataloged tool is not a row",
            tools_head + "We run [cspell][cspell-link] in CI.\n" + tools_defs,
            0,
        ),
    ]
    for label, text, wantn in tool_cases:
        got = third_party_tool_findings(text, cat)
        if len(got) != wantn:
            ok = False
        print(
            f"  {'ok  ' if len(got) == wantn else 'FAIL'} want={wantn} got={len(got)}  3rd party tools: {label}"
        )
        if len(got) != wantn:
            for _, t in got:
                print(f"         {t}")

    # A canonicalLinks entry that can match nothing is a name nothing enforces, so it raises rather than skipping.
    # Skipping would quietly stop checking that destination, which is the failure mode this whole dimension exists to avoid.
    broken = dict(rm, canonicalLinks=list(rm["canonicalLinks"]) + [{"name": "orphan-link"}])
    try:
        canonical_link_entry("https://example.test/", broken, "o/r")
        ok = False
        print("  FAIL canonicalLinks entry with neither repoPath nor match: no error raised")
    except KeyError as e:
        if "readme-sections.json" not in str(e) or "orphan-link" not in str(e):
            ok = False
            print(f"  FAIL canonicalLinks entry error does not name the file and entry -> {e}")
        else:
            print(
                "  ok   canonicalLinks entry with neither repoPath nor match: raises, naming the file and the entry"
            )

    # The tagline is the first line of the intro region, so a README carrying a second paragraph still mirrors.
    two_para = title_and_intro("# X\n\nThe tagline.\n\nA clarifying paragraph.\n\n## Next\n")[1]
    if tagline(two_para) != "The tagline." or "\n" in tagline(two_para):
        ok = False
        print(f"  FAIL tagline extraction -> {tagline(two_para)!r}")
    else:
        print("  ok   tagline: the first line of the intro region, further paragraphs excluded")

    # description_findings(): the README/About/Docker Hub mirror set, with and without a declared registry field.
    desc_readme = {"README.md": "# Fixture\n\nA short tagline.\n"}
    desc_cases = [
        (
            "no declared field, About matches the README tagline",
            desc_readme,
            {},
            {"description": "A short tagline."},
            0,
        ),
        (
            "no declared field, About mismatches the README tagline",
            desc_readme,
            {},
            {"description": "Something else."},
            1,
        ),
        (
            "declared field matches both README and About",
            desc_readme,
            {"description": "A short tagline."},
            {"description": "A short tagline."},
            0,
        ),
        (
            "declared field present, README tagline diverges from it",
            desc_readme,
            {"description": "The declared description."},
            {"description": "The declared description."},
            1,
        ),
        (
            "declared field present, About diverges from it",
            desc_readme,
            {"description": "A short tagline."},
            {"description": "Something else."},
            1,
        ),
        (
            "declared field present, no README at all, About diverges from it",
            {},
            {"description": "The declared description."},
            {"description": "Something else."},
            1,
        ),
        (
            "declared field present, no README at all, About already matches it",
            {},
            {"description": "The declared description."},
            {"description": "The declared description."},
            0,
        ),
        (
            "no declared field and no README, nothing to measure against",
            {},
            {},
            {"description": "Anything."},
            0,
        ),
        (
            "a non-string declared field is reported rather than crashing",
            desc_readme,
            {"description": 42},
            {"description": "A short tagline."},
            1,
        ),
        (
            "an explicit null declared field is reported rather than read as absent",
            desc_readme,
            {"description": None},
            {"description": "A short tagline."},
            1,
        ),
        (
            "a whitespace-only declared field is reported rather than read as absent",
            desc_readme,
            {"description": "   "},
            {"description": "A short tagline."},
            1,
        ),
        (
            "a padded declared field is a DEFECT rather than silently trimmed",
            desc_readme,
            {"description": "  A short tagline.  "},
            {"description": "A short tagline."},
            1,
        ),
        (
            "a declared field with an embedded newline is a DEFECT rather than silently accepted",
            desc_readme,
            {"description": "A short tagline.\nA second line."},
            {"description": "A short tagline."},
            1,
        ),
        (
            "a declared field carrying a Markdown link is a DEFECT rather than silently canonical",
            desc_readme,
            {"description": "See [docs](https://example.test) for more."},
            {"description": "A short tagline."},
            1,
        ),
        (
            "a declared field over the 100-char cap is a DEFECT rather than silently canonical",
            desc_readme,
            {"description": "a" * 101},
            {"description": "A short tagline."},
            1,
        ),
    ]
    for label, doc_texts_fx, entry_fx, live_fx, wantn in desc_cases:
        got = description_findings(doc_texts_fx, entry_fx, live_fx, "owner/Fixture")
        if len(got) != wantn:
            ok = False
        print(
            f"  {'ok  ' if len(got) == wantn else 'FAIL'} want={wantn} got={len(got)}  description: {label}"
        )
        if len(got) != wantn:
            for _, t in got:
                print(f"         {t}")
    # A null declared field is a DEFECT via validate.py's own contract, not a silent LETTER-only "About mismatch".
    null_declared = description_findings(
        desc_readme, {"description": None}, {"description": "A short tagline."}, "owner/Fixture"
    )
    if not any(
        k == "DEFECT" and "description must be a non-empty string" in t for k, t in null_declared
    ):
        ok = False
        print(f"  FAIL description: null-declared-field DEFECT contract -> {null_declared}")
    else:
        print("  ok   description: a null declared field is a DEFECT via validate.py's contract")
    # The DEFECT names the actual repo, not a generic "registry" label, so it stays actionable in a fleet-wide run.
    if not any(k == "DEFECT" and t.startswith("owner/Fixture:") for k, t in null_declared):
        ok = False
        print(f"  FAIL description: DEFECT does not name the repo -> {null_declared}")
    else:
        print("  ok   description: a declared-field DEFECT names the repo, not a generic label")
    # The declared field, once present, is what the wording names as the source - not "the README".
    declared_mismatch = description_findings(
        desc_readme,
        {"description": "The declared description."},
        {"description": "Something else."},
        "owner/Fixture",
    )
    if not any(
        "declared description" in t and "registry/repos.json" in t for _, t in declared_mismatch
    ):
        ok = False
        print(f"  FAIL description: declared-field wording -> {declared_mismatch}")
    else:
        print(
            "  ok   description: a declared field names registry/repos.json as the source, not the README"
        )
    # A docker-publishing repo's Docker Hub short description is checked against the same canonical value.
    real_dhd = globals()["docker_hub_description"]
    globals()["docker_hub_description"] = lambda slug: "A stale Docker Hub blurb."
    try:
        docker_mismatch = description_findings(
            desc_readme,
            {"description": "A short tagline.", "publish": [{"target": "docker"}]},
            {"description": "A short tagline."},
            "owner/Fixture",
        )
    finally:
        globals()["docker_hub_description"] = real_dhd
    if not any("Docker Hub short description" in t for _, t in docker_mismatch):
        ok = False
        print(f"  FAIL description: Docker Hub mismatch not reported -> {docker_mismatch}")
    else:
        print("  ok   description: a docker repo's stale Docker Hub short description is reported")

    # A ground-truth branch that does not resolve is one error, not a baseline's worth of letters.
    # Every `?ref=` read would 404 and report each carried file absent, describing the ref, not the repo.
    # The branch facts are already read at that point, so they are reported rather than dropped.
    entry = {"name": "Fixture", "url": "https://github.com/owner/Fixture", "hasDevelop": True}
    real_gh = globals()["gh"]
    globals()["gh"] = lambda path, ok404=False: None if "/branches/" in path else {"private": False}
    try:
        missing_findings, missing_sha = audit_repo(entry, {"registry": {}}, "no-such-branch")
    finally:
        globals()["gh"] = real_gh
    kinds = [k for k, _ in missing_findings]
    errors = [t for k, t in missing_findings if k == "ERROR"]
    if (
        missing_sha != ""
        or len(errors) != 1
        or "no-such-branch" not in errors[0]
        or kinds != ["DEFECT", "DRIFT", "ERROR"]
    ):
        ok = False
        print(f"  FAIL missing ground branch -> {missing_findings}")
    else:
        print(
            "  ok   missing ground branch: the branch facts, then one error naming the ref, no letters"
        )

    # The hub-only set is the manifest subtracted from the hub's tracked files, so a declared path must never appear in it.
    # Asserted against the live manifest rather than a fixture, since the failure this guards is a declared path leaking into the deletion list, which only the real pairing can show.
    declared = {e["path"] for e in load("spec/files.json")["baseline"]}
    hub_only = hub_only_paths({"files": load("spec/files.json")}, rev="HEAD")
    leaked = sorted(declared & hub_only)
    if leaked or not hub_only:
        ok = False
        print(
            f"  FAIL hub-only set -> {len(hub_only)} paths, {len(leaked)} declared leaked: {leaked}"
        )
    else:
        print(
            f"  ok   hub-only set excludes every declared path ({len(hub_only)} hub-hosted paths, {len(declared)} declared)"
        )

    # A malformed ledger row is dropped rather than raising, so a hand-edit cannot take the audit down with it.
    # The well-formed rows around it still resolve, which is what keeps a typo from silently clearing every disposition.
    led = {
        "divergences": {
            "gaps": [
                {"path": "a/b.sh", "disposition": "retire", "reason": "r"},
                {"path": "c/d.md", "disposition": "accepted"},
                {"path": "no-disposition.md"},
                {"disposition": "retire"},
                "not-an-object",
            ]
        }
    }
    got_disp = gap_dispositions(led)
    if got_disp != {"a/b.sh": ("retire", "r"), "c/d.md": ("accepted", "")}:
        ok = False
        print(f"  FAIL gap disposition parsing -> {got_disp}")
    else:
        print(
            "  ok   gap dispositions: well-formed rows parse, a row missing path or disposition is dropped"
        )

    # A truncated or unreadable tree returns None so the caller reports it, rather than a partial set that reads as a repo carrying nothing.
    # A partial tree cannot tell a file the repo does not have from one the response stopped short of, and the second is the silent-narrowing case.
    real_gh = globals()["gh"]
    head = {"commit": {"commit": {"tree": {"sha": "deadbeef"}}}}
    try:
        globals()["gh"] = lambda path, ok404=False: {
            "tree": [{"path": "x", "type": "blob"}],
            "truncated": True,
        }
        truncated = repo_tree("o/r", head)
        globals()["gh"] = lambda path, ok404=False: {
            "tree": [{"path": "x", "type": "blob"}, {"path": "d", "type": "tree"}],
            "truncated": False,
        }
        whole = repo_tree("o/r", head)
    finally:
        globals()["gh"] = real_gh
    if truncated is not None or repo_tree("o/r", {"commit": {}}) is not None or whole != {"x"}:
        ok = False
        print(f"  FAIL repo_tree -> truncated={truncated}, whole={whole}")
    else:
        print(
            "  ok   repo_tree: a truncated tree and a missing tree sha both return None, and a whole one drops non-blobs"
        )

    id_cases = [
        ("https://github.com/owner/Repo", "owner/repo"),
        ("https://github.com/owner/Repo/", "owner/repo"),
        ("  https://github.com/owner/Repo  ", "owner/repo"),
        ("https://github.com/owner/Repo.git", "owner/repo"),
        ("https://github.com/owner/Repo.github", "owner/repo.github"),
        ("https://github.com/owner/Repo?tab=readme", None),
        ("https://github.com/owner/Repo#readme", None),
        ("https://github.com/owner/Repo.git?tab=readme", None),
        ("https://github.com/owner?tab=readme/Repo", None),
        ("https://github.com/owner#readme/Repo", None),
        ("http://github.com/owner/Repo", None),
        ("https://gitlab.com/owner/Repo", None),
        ("not a url", None),
        (None, None),
        (42, None),
    ]
    id_ok = all(repo_identity(url) == want for url, want in id_cases)
    if id_ok:
        print(
            "  ok   repo_identity: parses owner/repo from a github.com URL, lowercased and trimmed"
        )
    else:
        ok = False
        got_ids = [(url, repo_identity(url)) for url, _ in id_cases]
        print(f"  FAIL repo_identity -> {got_ids}")

    # owner_repos: forks are dropped, and pagination stops the moment a page comes back short of 100.
    real_gh = globals()["gh"]
    try:
        pages = {
            1: [{"name": f"r{i}", "fork": False} for i in range(100)],
            2: [{"name": "kept", "fork": False}, {"name": "dropped-fork", "fork": True}],
        }
        globals()["gh"] = lambda path, ok404=False: (
            {"login": "owner"} if path == "user" else pages[int(path.rsplit("page=", 1)[1])]
        )
        got = {r["name"] for r in owner_repos("owner")}
    finally:
        globals()["gh"] = real_gh
    want = {f"r{i}" for i in range(100)} | {"kept"}
    if got != want:
        ok = False
        print(
            f"  FAIL owner_repos -> {len(got)} repos, expected {len(want)}, forks/pagination wrong"
        )
    else:
        print("  ok   owner_repos: forks dropped, pagination stops on a short page")

    # A gh("user") call that returns None (an empty response body, per its own docstring) must
    # raise the clear ownership-mismatch error, not crash with AttributeError on .get().
    try:
        globals()["gh"] = lambda path, ok404=False: None if path == "user" else []
        owner_repos("owner")
        ok = False
        print("  FAIL owner_repos: a None gh('user') response did not raise")
    except AttributeError:
        ok = False
        print("  FAIL owner_repos: a None gh('user') response crashed with AttributeError")
    except RuntimeError as exc:
        # Any RuntimeError would pass a bare except clause, including an unrelated one this test
        # was never meant to exercise. Assert the actual ownership-mismatch text.
        if "not registry owner 'owner'" not in str(exc):
            ok = False
            print(f"  FAIL owner_repos: a None gh('user') response raised the wrong error -> {exc}")
        else:
            print("  ok   owner_repos: a None gh('user') response raises the clear mismatch error")
    finally:
        globals()["gh"] = real_gh

    # GitHub logins are case-insensitive, so a registry owner spelled with different casing than
    # gh's own login must still be accepted as the same account.
    try:
        globals()["gh"] = lambda path, ok404=False: {"login": "Owner"} if path == "user" else []
        owner_repos("owner")
        print("  ok   owner_repos: a differently-cased login still matches the registry owner")
    except RuntimeError as e:
        ok = False
        print(f"  FAIL owner_repos: a differently-cased login was wrongly rejected -> {e}")
    finally:
        globals()["gh"] = real_gh

    # A None page mid-pagination (an empty response body) must fail loud rather than being read
    # as a short page: `or []` there would silently truncate the sweep and report clean.
    try:
        globals()["gh"] = lambda path, ok404=False: {"login": "owner"} if path == "user" else None
        owner_repos("owner")
        ok = False
        print("  FAIL owner_repos: a None page did not raise")
    except RuntimeError:
        print("  ok   owner_repos: a None page fails loud instead of reading as an empty page")
    finally:
        globals()["gh"] = real_gh

    real_owner_repos = globals()["owner_repos"]
    try:
        cases = [
            (
                "a repo missing from the registry is a DEFECT",
                [{"name": "New", "full_name": "owner/New", "archived": False}],
                {"owner": "owner", "repos": []},
                [("DEFECT", "owner/New: exists on GitHub with no registry/repos.json entry")],
            ),
            (
                "archived on GitHub but not in the registry is DRIFT",
                [{"name": "Old", "full_name": "owner/Old", "archived": True}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Old",
                            "url": "https://github.com/owner/Old",
                            "status": "cataloged",
                        }
                    ],
                },
                [
                    (
                        "DRIFT",
                        "Old: archived on GitHub but registry status is 'cataloged' (reconcile to 'archived')",
                    )
                ],
            ),
            (
                "registry says archived but GitHub disagrees is DRIFT",
                [{"name": "Back", "full_name": "owner/Back", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Back",
                            "url": "https://github.com/owner/Back",
                            "status": "archived",
                        }
                    ],
                },
                [
                    (
                        "DRIFT",
                        "Back: registry status is 'archived' but GitHub reports it unarchived "
                        + "(reconcile status, or verify it was not unarchived by mistake)",
                    )
                ],
            ),
            (
                "matched, not archived either side, is clean",
                [{"name": "Fine", "full_name": "owner/Fine", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Fine",
                            "url": "https://github.com/owner/Fine",
                            "status": "cataloged",
                        }
                    ],
                },
                [],
            ),
            (
                "identity matching is case-insensitive",
                [{"name": "MixedCase", "full_name": "Owner/MixedCase", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "mixedcase",
                            "url": "https://github.com/owner/mixedcase",
                            "status": "cataloged",
                        }
                    ],
                },
                [],
            ),
            (
                "an incidental URL whitespace difference does not miss a real match",
                [{"name": "Spacey", "full_name": "owner/Spacey", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Spacey",
                            "url": "  https://github.com/owner/Spacey  ",
                            "status": "cataloged",
                        }
                    ],
                },
                [],
            ),
            (
                "a same-named repo under a different owner is not treated as a match",
                [{"name": "Shared", "full_name": "owner/Shared", "archived": False}],
                {
                    "owner": "owner",
                    "repos": [
                        {
                            "name": "Shared",
                            "url": "https://github.com/someone-else/Shared",
                            "status": "cataloged",
                        }
                    ],
                },
                [("DEFECT", "owner/Shared: exists on GitHub with no registry/repos.json entry")],
            ),
        ]
        for label, gh_repos, spec_registry, want in cases:
            globals()["owner_repos"] = lambda owner, r=gh_repos: r
            got = membership_findings({"registry": spec_registry})
            if got != want:
                ok = False
                print(f"  FAIL membership: {label} -> {got}")
            else:
                print(f"  ok   membership: {label}")
    finally:
        globals()["owner_repos"] = real_owner_repos

    # An intentRef with an anchor resolves to the whole hub file, not the anchor-qualified name git cannot look up, and reference still wins where the manifest sets both.
    canonical_cases = [
        (
            "no reference or intentRef falls back to the file's own path",
            {},
            "AGENTS.md",
            "AGENTS.md",
        ),
        (
            "an intentRef equal to the path is itself the canonical",
            {"intentRef": "GOVERNANCE.md"},
            "GOVERNANCE.md",
            "GOVERNANCE.md",
        ),
        (
            "an anchored intentRef strips the anchor and keeps the whole file",
            {"intentRef": "GOVERNANCE.md#line-endings"},
            ".editorconfig",
            "GOVERNANCE.md",
        ),
        (
            "reference wins over intentRef when the manifest sets both",
            {"reference": "catalog/snippets/configs/codecov.yml", "intentRef": "WORKFLOW.md"},
            "codecov.yml",
            "catalog/snippets/configs/codecov.yml",
        ),
        (
            "a literal '#' in reference is preserved rather than treated as an anchor",
            {"reference": "docs/notes#1.md"},
            "notes.md",
            "docs/notes#1.md",
        ),
        (
            "a literal '#' in path is preserved rather than treated as an anchor",
            {},
            "docs/notes#1.md",
            "docs/notes#1.md",
        ),
        (
            "a non-string intentRef falls back to path instead of crashing",
            {"intentRef": 123},
            "AUDIT.md",
            "AUDIT.md",
        ),
        (
            "an anchor-only intentRef falls back to path rather than an empty canonical",
            {"intentRef": "#line-endings"},
            "AUDIT.md",
            "AUDIT.md",
        ),
    ]
    for label, item, path, want in canonical_cases:
        got = intent_canonical_rel(item, path)
        good = got == want
        if not good:
            ok = False
        print(f"  {'ok  ' if good else 'FAIL'} intent_canonical_rel: {label} -> {got!r}")

    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


def render_issue(entry, findings, ground, audited_sha, run_utc, hub_sha):
    """A ready-to-file convergence issue (title, body) generated from one repo's audit findings.

    The content is a view over the audit, not composed by hand, so it is always accurate and regenerable -
    re-run the audit, re-generate the issue. Findings are grouped by what the maintainer does with them:
    presence/contract findings to fix, drift to converge (re-vendor or review), and anything unverifiable.
    """
    name = entry["name"]
    types = ", ".join(entry.get("types", [])) or "untyped"
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
    wanted = {n.lower() for n in a.names}
    repos = [r for r in spec["registry"]["repos"] if r.get("status") == "cataloged"]
    if wanted:
        repos = [r for r in repos if r["name"].lower() in wanted]
        missing = wanted - {r["name"].lower() for r in repos}
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
        # An unverifiable audit still produces an honest issue.
        except Exception as e:  # noqa: BLE001
            findings, audited_sha = [("ERROR", str(e))], ""
        title, body = render_issue(
            entry, findings, ground_branch_of(entry, a.branch), audited_sha, run_utc, hub_sha
        )
        print(title)
        print(
            body, end=""
        )  # line 1 is the bare title, the body starts on line 2 - head -1 / tail -n +2 friendly
        return 0

    override = f" | branch override {a.branch}" if a.branch else ""
    print(f"audit run {run_utc} | hub {hub_sha}{override}")
    if not HUB_NAME_FROM_REMOTE:
        print(
            f"warning: no git remote; template-reference check falls back to the directory name '{HUB_NAME}' and may miss",
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
        ground = ground_branch_of(entry, a.branch)
        try:
            findings, audited_sha = audit_repo(entry, spec, a.branch)
        # A gh/JSON failure mid-audit must not abort the sweep.
        except Exception as e:  # noqa: BLE001
            findings, audited_sha = [("ERROR", str(e))], ""
        stamp = f" @ {ground}@{audited_sha[:7]}" if audited_sha else ""
        print(f"== {entry['name']} ({', '.join(entry.get('types', []))}; {model}){stamp} ==")
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
