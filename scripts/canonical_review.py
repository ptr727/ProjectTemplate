#!/usr/bin/env python3
"""Record and check full-content reviews of the canonical content this hub authors and other repos carry.

The problem this exists for is an ordering one. Hub-owned content under `.agents/skills/`,
`GOVERNANCE.md`, `WORKFLOW.md`, `AGENTS.md` and `AUDIT.md` is written, reviewed and merged here
against a diff that is usually a few lines. The whole tree only ever reaches a reviewer as new
files, in full, when a downstream repository carries it for the first time. So the first real
read of a rule happens in a repository that cannot act on the result: the tree is manifest-owned,
`scripts/carry.py check` compares source and target digests, and a local edit there becomes drift
on the next fidelity check. Every carrier then re-discovers the same defect, and the finding
arrives in a session with no hub checkout and no standing to test the claim
(ptr727/ProjectTemplate#1138).

This module reproduces the carrier's read here, before a carrier performs it, and keeps a record
of which content has had one.

**A unit is what a reviewer reads whole.** For a Markdown canonical, that is one level-two
section, which is also the fidelity unit `spec/section-model.md` declares and the unit key
`spec/divergences.json` already uses (`<path> > <section>`). For anything else it is the file.
Splitting by section is what keeps the read proportionate: a reviewer asked for the whole of
`GOVERNANCE.md` on every edit reads none of it, where a reviewer asked for the one section an
edit lands in reads all of it.

**Coverage is keyed on content, never on a commit.** A unit is covered when a recorded pass names
its current digest. Editing the unit invalidates the pass, because the text the reviewer read is
no longer the text a carrier will receive. Editing a neighbouring section does not, because that
reviewer's read of this one is still a read of these bytes.

**The gate is on what a branch changes, and the backlog is reported rather than gated.** Most
units have never had a full read here, which is the defect #1138 records rather than a reason to
block every push until it is worked off. `check` refuses only the units this branch's own diff
moved, so the ordering is fixed going forward, and `report` renders what is left as a burn-down
the way `reports/divergences.md` does for fidelity.

The verdict vocabulary is `scripts/local_review.py`'s, because both gates run from the same
pre-push hook and a caller reading an exit code must not have to know which one answered: 0 is
covered, 1 is a finding, and 2 is the check itself not having run.

Usage:
    python3 scripts/canonical_review.py list                 every unit and its digest, as JSON
    python3 scripts/canonical_review.py status               what is covered, stale, or never read
    python3 scripts/canonical_review.py check                gate this branch's changed units
    python3 scripts/canonical_review.py record --reviewer agent-skill --unit '<key>=<digest>'
    python3 scripts/canonical_review.py report               write reports/canonical-review.md
"""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# `spec/audit.py` is a script rather than a package module, and it imports its own sibling by bare name, so the spec directory has to be importable before it is.
sys.path.insert(0, str(ROOT / "spec"))

import audit
import build_dist
import carry
import local_review

# Reused rather than restated.
# Two gates in one pre-push hook that disagree about which branch "develop" means, or about what an exit code says, are a defect in the pair rather than in either.
CannotRun = local_review.CannotRun
DEFAULT_TARGET = local_review.DEFAULT_TARGET
EXIT_COVERED = local_review.EXIT_COVERED
EXIT_NOT_COVERED = local_review.EXIT_NOT_COVERED
EXIT_CANNOT_RUN = local_review.EXIT_CANNOT_RUN
emit = local_review.emit
git = local_review.git

MANIFEST = "spec/files.json"
LEDGER = "reports/canonical-review.json"
REPORT = "reports/canonical-review.md"

LEDGER_NOTE = (
    "What full-content reviews of hub canonical content have covered, one entry per unit, holding"
    " the most recent pass. Written by scripts/canonical_review.py record, never by hand, and git"
    " keeps the history. A unit is covered while its digest here matches the content's, so an edit"
    " to the unit retires the pass. See ptr727/ProjectTemplate#1138 for why the record exists."
)

# The fidelity values that mean the hub authored the content and a downstream repository receives it.
# `interface` is deliberately absent: that names a contract whose body is the repository's own, per RESYNC.md "Apply, in This Order", so there is no hub text for a carrier to re-read.
# An entry declaring no fidelity at all is a presence requirement over the repository's own content, which is likewise nothing this hub authored.
AUTHORED_FIDELITY = frozenset({"verbatim", "intent"})
TREE_FIDELITY = frozenset({"verbatim-tree"})

# `spec/divergences.json`'s own unit key delimiter, so one vocabulary names a section across the fidelity ledger, this one, and any finding written against either.
SECTION_DELIM = " > "
# The region before a document's first level-two heading is content a carrier reads like any other, so it is a unit rather than a gap between them.
# A literal `## (preamble)` heading would collide with it, which file_units then refuses as a duplicate rather than resolving, since only one of the two is a section.
PREAMBLE = "(preamble)"

# The generator's own source-to-distribution mapping, read from it rather than restated here.
# A downstream repository carries `.github/skills/`, and a defect found in one is fixed in `.agents/skills/`, which `scripts/build_dist.py --check` then holds the carried copy equal to.
# Keying the unit on the generated path would name a file no fix may edit.
GENERATED_SKILLS = build_dist.GITHUB_SKILLS.relative_to(build_dist.ROOT).as_posix()
AUTHORED_SKILLS = build_dist.SKILLS_SRC.relative_to(build_dist.ROOT).as_posix()

# What `record` accepts as a reviewer, which is `local_review.py`'s vocabulary for the same reason the exit codes are: a pass over a unit is performed by the same kinds of reviewer as a pass over a branch diff, and two spellings of one reviewer make the two records impossible to read together.
REVIEWERS = local_review.BACKENDS


def normalize(text: str) -> str:
    """A unit's comparable form, which neutralizes line endings and nothing else.

    Deliberately not `spec/audit.py`'s `normalize`, which also masks Dependabot-owned action pins
    and per-repo `needs:` lists. Those are governed drift for a fidelity comparison, where the
    question is whether two copies say the same thing. The question here is whether a reviewer has
    read these bytes, and masking any of them would report a read of text nobody saw. Line endings
    are the one exception, because the reviewer's read is identical either way and `.gitattributes`
    governs them separately.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def digest(text: str) -> str:
    """The content key a pass is recorded against."""
    return "sha256:" + hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def decode(rel: str, data: bytes) -> str:
    """A carried file's text, refusing rather than guessing at content that is not UTF-8.

    Every canonical is text the fleet's own charset tiers govern, so a decode failure is a broken
    input rather than a file to skip. Skipping it would drop its units out of the set silently,
    which is the narrowing GOVERNANCE.md "Verification Discipline" names.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CannotRun(f"{rel} is not valid UTF-8, so its units cannot be read: {exc}") from exc


def split_markdown(text: str) -> list[tuple[str | None, str]]:
    """`(heading, region)` pairs: the preamble under `None`, then one region per level-two heading.

    Fence state comes from `spec/audit.py`'s own step function rather than a second reading of
    CommonMark, so a `## ` shown inside a code sample cannot split a unit here while leaving the
    fidelity checks reading the document the other way. A document read two ways by two tools is
    the failure that helper was extracted to end.

    The heading line's exact bytes stay inside its region, so re-casing or re-spacing a heading
    surfaces as a changed unit rather than as an invisible edit.
    """
    regions: list[tuple[str | None, list[str]]] = [(None, [])]
    marker: str | None = None
    marker_len = 0
    for line in normalize(text).split("\n"):
        marker, marker_len, boundary = audit._fence_step(line, marker, marker_len)
        stripped = line.strip()
        if not boundary and marker is None and stripped.startswith("## "):
            regions.append((stripped[2:].strip(), [line]))
            continue
        regions[-1][1].append(line)
    # A document whose first heading is its first line has an empty preamble, which is a region holding nothing rather than a unit a reviewer could read.
    if not regions[0][1] or not "\n".join(regions[0][1]).strip():
        regions.pop(0)
    return [(heading, "\n".join(lines)) for heading, lines in regions]


def file_units(rel: str, text: str) -> dict[str, str]:
    """Every unit one carried file contributes, as key -> text.

    A Markdown canonical splits into its sections, and anything else is one unit, because a section
    is a Markdown notion and a config file has no comparable seam to read whole.
    """
    if not rel.endswith(".md"):
        return {rel: text}
    regions = split_markdown(text)
    # A Markdown file with no level-two heading at all is one region, and naming that region `<path> > (preamble)` would claim a structure the document does not have.
    if len(regions) == 1 and regions[0][0] is None:
        return {rel: regions[0][1]}
    out: dict[str, str] = {}
    seen: dict[str, str] = {}
    for heading, region in regions:
        name = heading if heading is not None else PREAMBLE
        key = f"{rel}{SECTION_DELIM}{name}"
        # Two sections of one name are two answers to one question, and keeping the last would record a read of one of them as covering both.
        # Case-folded, because the declared-name lookup folds and spec/audit.py's heading match folds, so a pair differing only in case is one name to every reader but this one.
        earlier = seen.get(key.lower())
        if earlier is not None:
            shown = f"'{earlier}'" if earlier == name else f"'{earlier}' and '{name}'"
            raise CannotRun(
                f"{rel} carries two level-two sections named {shown},"
                " so a review of one cannot be told from a review of the other"
            )
        seen[key.lower()] = name
        out[key] = region
    return out


def authored_source(entry: dict[str, Any]) -> str:
    """The path a manifest tree entry's content is hand-authored at, which is where a fix lands."""
    source = str(entry.get("source", ""))
    return AUTHORED_SKILLS if source == GENERATED_SKILLS else source


def declared_sections(entry: dict[str, Any]) -> list[str] | None:
    """The sections an entry restricts its carry to, or None where the whole file carries.

    A downstream copy carries the sections the manifest declares and no others, which
    `spec/audit.py`'s undeclared-heading check is what enforces, so a section this hub keeps for
    itself is not content any carrier ever reads. Demanding a carrier's read of one would be this
    tool inventing an obligation the manifest does not state, and the two hub-only sections of
    `GOVERNANCE.md` are exactly that case. An entry marked `whole` carries the file entire, and its
    `sections` list is then a presence requirement inside that carry rather than a narrowing of it.
    """
    if entry.get("whole"):
        return None
    names = [
        elt if isinstance(elt, str) else str(elt.get("name", ""))
        for elt in entry.get("sections", [])
    ]
    return [name for name in names if name] or None


def parse_manifest(rel: str, data: bytes) -> dict[str, Any]:
    """The manifest, refused rather than guessed at when it cannot be read."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CannotRun(f"cannot read {rel}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CannotRun(f"{rel} is not an object, so no carried set can be read from it")
    return payload


def tracked_files(root: Path, commit: str | None = None) -> set[str]:
    """Every path git tracks, in this working tree or at `commit`.

    Git's view rather than the filesystem's, on both sides, because they disagree in ways that
    matter here. A filesystem walk of a carried tree picks up whatever happens to be sitting in it,
    so a gitignored `.DS_Store` or an editor backup becomes canonical content: the first fails the
    UTF-8 decode and takes every subcommand to exit 2, blocking every push from that clone, and the
    second becomes a unit the gate demands a review pass for and `record` writes into the ledger
    forever. Neither is content any repository carries. Reading the base side from git's tree while
    reading this side from disk also compares two different notions of membership, which is the
    kind of asymmetry that reports a change nobody made.
    """
    if commit is None:
        # Tracked plus untracked-and-not-ignored, rather than tracked alone.
        # A carried file this branch has created but not staged is content a carrier will receive, and dropping it would narrow the gate exactly where a new canonical is added.
        # Ignored paths stay out, which is what keeps a stray .DS_Store or build artifact from becoming canonical content.
        listing = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", root=root)
    else:
        listing = git("ls-tree", "-r", "--name-only", "-z", commit, root=root)
    return {path for path in listing.split("\0") if path}


def select_carried(
    manifest: dict[str, Any], tracked: set[str]
) -> tuple[dict[str, list[str] | None], list[str]]:
    """What a manifest declares carried over `tracked`, and what it declares that is not there.

    Parameterized on the path set rather than reading one tree directly, because the base side of a
    comparison has to be resolved against the base commit's own manifest and its own tree. Reading
    one manifest against two trees is the defect this shape exists to prevent: a branch that adds an
    already-present file to the manifest makes content newly carried without changing a byte of it,
    and a single-manifest comparison scores that as no change at all.

    Each value is the section list that path's carry is restricted to, or None where the whole file
    carries. A declared path the tree does not hold is reported rather than skipped quietly, being
    the ordinary case for a file scoped to a project type this hub is not, and also what a manifest
    entry pointing at nothing looks like.
    """
    carried: dict[str, list[str] | None] = {}
    absent: list[str] = []
    for entry in manifest.get("baseline", []):
        if entry.get("fidelity") not in AUTHORED_FIDELITY:
            continue
        rel = str(entry.get("path", ""))
        if rel.startswith("/") or ".." in pathlib.PurePosixPath(rel).parts:
            raise CannotRun(f"{MANIFEST} declares a path that is not repository-relative: {rel}")
        if rel not in tracked:
            if rel not in absent:
                absent.append(rel)
            continue
        sections = declared_sections(entry)
        if rel not in carried:
            carried[rel] = sections
            continue
        # Two entries naming one file are two parts of one carry rather than two carries, and a whole-file part subsumes a sectioned one rather than competing with it.
        previous = carried[rel]
        carried[rel] = None if previous is None or sections is None else [*previous, *sections]
    for entry in manifest.get("trees", []):
        if entry.get("fidelity") not in TREE_FIDELITY:
            continue
        # Membership comes from the tree a repository actually receives, and the key from the tree a fix is allowed to edit.
        # Taking both from the authored side would demand a carrier's read of a file no carrier ever gets, which `.agents/skills/README.md` is: it exists only on the authored side and `build_dist.py` copies nothing of it into the distribution.
        declared = str(entry.get("source", ""))
        authored = authored_source(entry)
        patterns = list(entry.get("include", ["**/*"]))
        prefix = f"{declared}/"
        found = sorted(
            path[len(prefix) :]
            for path in tracked
            if path.startswith(prefix) and carry.included(path[len(prefix) :], patterns)
        )
        if not found:
            absent.append(declared)
            continue
        for rel in found:
            carried[f"{authored}/{rel}"] = None
    return carried, sorted(absent)


def carried_paths(root: Path) -> tuple[dict[str, list[str] | None], list[str]]:
    """What this working tree's own manifest declares carried, over what git tracks here."""
    reader = disk_reader(root)
    data = reader(MANIFEST)
    if data is None:
        raise CannotRun(
            f"this working tree holds no {MANIFEST}, so nothing describes what it carries"
        )
    return select_carried(parse_manifest(MANIFEST, data), tracked_files(root))


def carried_at(root: Path, commit: str) -> dict[str, list[str] | None]:
    """What the manifest at `commit` declared carried, read against that commit's own tree.

    A commit holding no manifest carried nothing, which is the honest answer for a base that
    predates the manifest and makes every unit on this branch a first read.
    """
    blobs = blobs_at(root, commit, [MANIFEST])
    if MANIFEST not in blobs:
        return {}
    carried, _ = select_carried(
        parse_manifest(MANIFEST, blobs[MANIFEST]), tracked_files(root, commit)
    )
    return carried


def build_units(
    carried: dict[str, list[str] | None],
    read: Callable[[str], bytes | None],
) -> tuple[dict[str, str], list[str]]:
    """Unit key -> text for the carried paths `read` can supply, plus declared sections it lacks.

    A path the reader has nothing for contributes no units, which is what makes one function serve
    both the working tree and a base commit: at the base, a file this branch adds is simply absent.
    A declared section the file does not hold is a different thing entirely, a manifest naming a
    heading that is not there, so it is reported rather than passed over.
    """
    units: dict[str, str] = {}
    missing: list[str] = []
    for rel in sorted(carried):
        data = read(rel)
        if data is None:
            continue
        found = file_units(rel, decode(rel, data))
        sections = carried[rel]
        if sections is None:
            units.update(found)
            continue
        # Case-folded, matching spec/audit.py's own heading match, so one re-cased declaration cannot make a section silently stop being a unit while the fidelity check still hashes it.
        # The key is the document's heading rather than the manifest's spelling, so what `check` prints is what the file holds.
        by_heading = {
            key.split(SECTION_DELIM, 1)[1].strip().lower(): key
            for key in found
            if SECTION_DELIM in key
        }
        for name in sections:
            key = by_heading.get(name.strip().lower())
            if key is None:
                missing.append(f"{rel}{SECTION_DELIM}{name}")
            else:
                units[key] = found[key]
    return units, sorted(set(missing))


def disk_reader(root: Path) -> Callable[[str], bytes | None]:
    """Read a carried path out of the working tree, refusing rather than reporting it absent.

    An unreadable file is a boundary and not an answer, since treating it as absent would drop its
    units out of the set and read as a branch that never carried them.
    """

    def read(rel: str) -> bytes | None:
        try:
            path = carry.relative_root(root, rel)
        except carry.CarryError as exc:
            raise CannotRun(f"{MANIFEST} declares an unusable path: {exc}") from exc
        if not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError as exc:
            raise CannotRun(f"cannot read {rel}: {exc}") from exc

    return read


def units(root: Path) -> tuple[dict[str, str], list[str]]:
    """This working tree's units and digests, plus what the manifest declares and it does not hold."""
    carried, absent = carried_paths(root)
    found, missing = build_units(carried, disk_reader(root))
    return {key: digest(text) for key, text in found.items()}, sorted(absent + missing)


def blobs_at(root: Path, commit: str, rels: list[str]) -> dict[str, bytes]:
    """The same files' bytes at `commit`, for the ones that exist there.

    One `git cat-file --batch` rather than a `git show` per file, because the carried set runs to
    dozens of paths and a process each is the difference between a hook a caller waits on and one
    they skip. The commit is verified to resolve before this runs, since every path of an
    unresolvable ref reports `missing`, which would read as a branch that introduced the entire
    canonical set rather than as a check that could not run.
    """
    if not rels:
        return {}
    # The batch protocol is one request per line, so a path holding a newline shifts every answer after it onto the wrong request.
    # That misreads as content rather than failing, which is the silent narrowing this refuses instead.
    # `--batch -z` would carry such a path, and it is not used because spec/host-tools.json declares no git floor that guarantees it.
    newlined = [rel for rel in rels if "\n" in rel or "\r" in rel]
    if newlined:
        raise CannotRun(
            f"a carried path holds a line ending in its name, which git cat-file --batch cannot be asked for: {newlined[0]!r}"
        )
    # Encoded with surrogateescape, matching how local_review.git decodes a path, so a name holding a non-UTF-8 byte round-trips instead of raising on the way back out.
    request = "".join(f"{commit}:{rel}\n" for rel in rels).encode("utf-8", "surrogateescape")
    # The same redirects local_review.git strips from every call it makes.
    # Inherited, GIT_OBJECT_DIRECTORY points this read at a store that does not hold the base commit's blobs, which reads as a base that carried nothing and refuses the push naming every unit in the tree as newly carried.
    env = {k: v for k, v in os.environ.items() if k not in local_review.INHERITED_REDIRECTS}
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "cat-file", "--batch"],
        cwd=str(root),
        env=env,
        input=request,
        capture_output=True,
        check=False,
        timeout=local_review.GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "no error text"
        raise CannotRun(f"git cat-file --batch failed: {detail}")
    out: dict[str, bytes] = {}
    buffer = proc.stdout
    position = 0
    for rel in rels:
        end = buffer.find(b"\n", position)
        if end < 0:
            raise CannotRun(
                f"git cat-file --batch stopped before answering for {rel},"
                " so the base content is unknown rather than absent"
            )
        header = buffer[position:end].decode("utf-8", "replace")
        position = end + 1
        fields = header.split(" ")
        # An answer is either `<oid> <type> <size>` or the request echoed back with a status word.
        # Keyed on the last field rather than the third, because git echoes the request verbatim and a path holding a space pushes a digit into third place.
        # `HEAD:a b 12 c.md missing` parsed as a 12-byte object and took the next answer's header as this path's content.
        if fields[-1] in ("missing", "ambiguous", "dangling"):
            continue
        if len(fields) != 3 or not fields[2].isdigit():
            raise CannotRun(
                f"git cat-file --batch answered for {rel!r} with a header this cannot read: {header!r}"
            )
        size = int(fields[2])
        out[rel] = buffer[position : position + size]
        # The payload is followed by a newline the header's size does not count.
        position += size + 1
    return out


def units_at(root: Path, commit: str) -> dict[str, str]:
    """The units the tree at `commit` carried, by that commit's own manifest.

    Its own manifest rather than this branch's, because the question is what this branch changed
    about what a carrier receives, and adding an already-present file or section to the manifest
    changes exactly that while changing no byte of the file. Measured against one manifest, such a
    branch reports no changed unit and makes content nothing has read available to every carrier,
    which is the first-read case this whole gate exists for.
    """
    try:
        carried = carried_at(root, commit)
        blobs = blobs_at(root, commit, sorted(carried))
        found, _ = build_units(carried, blobs.get)
    except CannotRun as exc:
        # Named, because every message underneath carries only a path and the working tree's copy of that path is usually fine.
        # A duplicate heading a later commit removed reads as a defect in the file the reader is about to open, which does not hold one.
        raise CannotRun(f"reading the base commit {commit[:12]}: {exc}") from exc
    return {key: digest(text) for key, text in found.items()}


def read_ledger(root: Path) -> dict[str, dict[str, Any]]:
    """The recorded passes, keyed by unit.

    An absent ledger is an empty record rather than a boundary, since the first repository to run
    this has nothing recorded yet and refusing there would make the gate impossible to adopt. An
    unreadable or malformed one is a boundary, because it is a record that exists and cannot be
    read, and treating that as empty would report every unit as never reviewed.
    """
    path = root / LEDGER
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CannotRun(f"cannot read {LEDGER}: {exc}") from exc
    passes = payload.get("passes") if isinstance(payload, dict) else None
    if not isinstance(passes, list):
        raise CannotRun(f"{LEDGER} has no 'passes' list, so no coverage can be read from it")
    out: dict[str, dict[str, Any]] = {}
    for entry in passes:
        if not isinstance(entry, dict) or not isinstance(entry.get("unit"), str):
            raise CannotRun(f"{LEDGER} holds an entry with no unit name")
        unit = entry["unit"]
        if unit in out:
            raise CannotRun(
                f"{LEDGER} holds two entries for '{unit}', so its coverage is two answers"
            )
        out[unit] = entry
    return out


def write_ledger(root: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Replace the ledger with `entries`, one per unit, ordered by unit key.

    Sorted and one entry per unit so a concurrent branch touching a different unit merges cleanly,
    and so the diff of a recorded pass reads as the pass rather than as a reordering.
    """
    payload = {"note": LEDGER_NOTE, "passes": [entries[unit] for unit in sorted(entries)]}
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    # Bytes rather than text, since a text-mode write would rewrite the file in the platform's own line ending, per GOVERNANCE.md "Verification Discipline".
    path.write_bytes((json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def state_of(unit: str, current: str, ledger: dict[str, dict[str, Any]]) -> str:
    """`covered`, `stale`, or `never`, for one unit."""
    entry = ledger.get(unit)
    if entry is None:
        return "never"
    return "covered" if entry.get("digest") == current else "stale"


def resolve_base(target: str | None, root: Path) -> tuple[str, str]:
    """The target name and the merge-base commit this branch's changes are measured from.

    A merge-base rather than the target's tip, for `scripts/local_review.py`'s reason: the two
    differ the moment the target moves, and measuring against the tip would report every unit the
    target gained since this branch forked as one this branch changed.
    """
    name = local_review.resolve_target(target)
    return name, local_review.merge_base(name, root)


def changed_units(root: Path, base: str) -> tuple[dict[str, str], list[str], list[str]]:
    """This branch's units, the ones a carrier would read differently than at `base`, and the absentees.

    A unit counts as changed when its text moved and when it is newly carried, since a carrier
    reads both for the first time and neither has been read here.
    """
    carried, absent = carried_paths(root)
    found, missing = build_units(carried, disk_reader(root))
    current = {key: digest(text) for key, text in found.items()}
    before = units_at(root, base)
    changed = sorted(unit for unit, value in current.items() if before.get(unit) != value)
    return current, changed, sorted(absent + missing)


def cmd_list(args: argparse.Namespace) -> int:
    root = local_review.repo_root()
    current, absent = units(root)
    emit(json.dumps({"units": current, "declaredButAbsent": absent}, indent=2))
    return EXIT_COVERED


def cmd_status(args: argparse.Namespace) -> int:
    root = local_review.repo_root()
    current, absent = units(root)
    ledger = read_ledger(root)
    states = {unit: state_of(unit, value, ledger) for unit, value in current.items()}
    counts = {
        state: sum(1 for s in states.values() if s == state)
        for state in ("covered", "stale", "never")
    }
    # Recorded against a unit the tree no longer holds, which is a renamed or deleted section whose entry is now unreachable.
    # Reported rather than pruned here, since deciding that a section is gone rather than moved is a reader's call.
    orphans = sorted(set(ledger) - set(current))
    emit(
        json.dumps(
            {
                "units": len(current),
                "counts": counts,
                "orphanedPasses": orphans,
                "declaredButAbsent": absent,
                "states": states,
            },
            indent=2,
        )
    )
    # Reports rather than gates, so a caller under `set -e` can run it whatever the answer is.
    return EXIT_COVERED


def cmd_check(args: argparse.Namespace) -> int:
    root = local_review.repo_root()
    target, base = resolve_base(args.target, root)
    current, changed, _ = changed_units(root, base)
    # Read before the empty-change answer, so an unreadable ledger still reports the boundary rather than being skipped into a verdict.
    ledger = read_ledger(root)
    if not changed:
        emit(f"No carried canonical unit changed against {target}, so there is nothing to cover.")
        return EXIT_COVERED
    uncovered = [unit for unit in changed if state_of(unit, current[unit], ledger) != "covered"]
    if not uncovered:
        emit(f"All {len(changed)} changed carried unit(s) are covered by a recorded pass.")
        return EXIT_COVERED
    emit(
        f"This branch changes {len(uncovered)} carried canonical unit(s)"
        " that no recorded pass covers at their current text:",
        sys.stderr,
    )
    # The key and the whole digest, in the shape `record` takes them, so closing the refusal is a copy rather than a second lookup against `list` over every unit in the tree.
    for unit in uncovered:
        emit(f"  {state_of(unit, current[unit], ledger):<7}  {unit}={current[unit]}", sys.stderr)
    emit(
        "\nA repository carrying this content reads each of these whole, as a new file, and cannot"
        "\nfix what it finds. Read each unit's whole current text, then record the pass, handing"
        "\nback the key and digest exactly as printed above:"
        f"\n  python3 scripts/canonical_review.py record --reviewer agent-skill"
        f" --target {target} --unit '<key>=<digest>'"
        '\nThe local-strict-review skill\'s "The Carried-Content Pass" says how the pass is run,'
        "\nand its refusal table what this refusal means where the units named look wrong.",
        sys.stderr,
    )
    return EXIT_NOT_COVERED


def parse_pairs(values: list[str]) -> dict[str, str]:
    """`--unit '<key>=<digest>'` arguments as a mapping.

    The digest is required rather than optional, and it is the caller passing back what it read
    before the review ran. Recording a unit by name alone would stamp whatever the file holds at
    record time, so an edit between the review and the record, a format-on-save or a hook autofix,
    would be attested to by a reviewer who never saw it. That is the one claim this record makes.
    """
    out: dict[str, str] = {}
    for value in values:
        key, _, want = value.rpartition("=")
        if not key or not want:
            raise CannotRun(f"--unit expects '<key>=<digest>', got '{value}'")
        if key in out:
            raise CannotRun(f"--unit names '{key}' twice")
        out[key] = want
    return out


def cmd_record(args: argparse.Namespace) -> int:
    if args.reviewer not in REVIEWERS:
        known = ", ".join(sorted(REVIEWERS))
        emit(f"unknown reviewer '{args.reviewer}'. Known reviewers: {known}", sys.stderr)
        return EXIT_CANNOT_RUN
    # A headless backend earns a pass by being run, since its completion event is what says it read anything.
    # This engine runs none, so recording one by hand would attest to a review that produced no evidence at all.
    if REVIEWERS[args.reviewer]["headless"]:
        emit(
            f"'{args.reviewer}' is headless, and this engine has no runner for one,"
            " so a pass over a canonical unit is recorded by the agent session that read it.",
            sys.stderr,
        )
        return EXIT_CANNOT_RUN
    if args.findings is not None and args.findings < 0:
        emit(f"--findings cannot be negative, got {args.findings}", sys.stderr)
        return EXIT_CANNOT_RUN
    root = local_review.repo_root()
    current, _ = units(root)
    wanted = parse_pairs(args.unit)
    unknown = sorted(set(wanted) - set(current))
    if unknown:
        emit("no such carried canonical unit:", sys.stderr)
        for unit in unknown:
            emit(f"  {unit}", sys.stderr)
        emit("Run 'canonical_review.py list' for the unit keys.", sys.stderr)
        return EXIT_CANNOT_RUN
    moved = sorted(unit for unit, want in wanted.items() if current[unit] != want)
    if moved:
        emit(
            "the content moved between the review and this record, so nothing was recorded:",
            sys.stderr,
        )
        for unit in moved:
            emit(
                f"  {unit}\n    reviewed {wanted[unit][:19]}\n    now      {current[unit][:19]}",
                sys.stderr,
            )
        emit("Re-read the unit's current text and record that.", sys.stderr)
        return EXIT_CANNOT_RUN
    ledger = read_ledger(root)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Stamped as the merge-base against the target, not HEAD, since a squash merge discards HEAD and an amend moves it.
    _, base = resolve_base(args.target, root)
    for unit, value in wanted.items():
        ledger[unit] = {
            "unit": unit,
            "digest": value,
            "reviewer": args.reviewer,
            "findings": args.findings,
            "hubCommit": base,
            "stamp": stamp,
        }
    write_ledger(root, ledger)
    # The burn-down is regenerated here rather than left to a caller to remember, since a ledger and a report that disagree are two answers about the same coverage.
    write_report(root)
    emit(f"recorded {args.reviewer} over {len(wanted)} unit(s) in {LEDGER}, and rewrote {REPORT}.")
    return EXIT_COVERED


def render_report(
    current: dict[str, str], ledger: dict[str, dict[str, Any]], absent: list[str]
) -> str:
    """The burn-down, grouped by the file a unit belongs to."""
    states = {unit: state_of(unit, value, ledger) for unit, value in current.items()}
    counts = {
        state: sum(1 for s in states.values() if s == state)
        for state in ("covered", "stale", "never")
    }
    lines = [
        "# Canonical content review coverage",
        "",
        (
            "Generated by `python3 scripts/canonical_review.py report`, and never hand-edited."
            " Records are written by `canonical_review.py record` into"
            " [`reports/canonical-review.json`][ledger]. Git dates this file."
        ),
        "",
        (
            "A unit is what a reviewer reads whole, decided by the carry manifest rather than by"
            " the document. In the ordinary case that is one level-two section of a carried"
            " Markdown canonical, and `canonical_review.py list` names the whole set."
            " It is **covered** when a recorded full-content pass names its current text, **stale**"
            " when a pass named earlier text, and **never** when no pass has read it here at all. A"
            " never-read unit is the backlog [ptr727/ProjectTemplate#1138][issue] records: the first"
            " real review of it happens in whichever repository carries it next."
        ),
        "",
        "## Coverage",
        "",
        f"- units: {len(current)}",
        f"- covered: {counts['covered']}",
        f"- stale: {counts['stale']}",
        f"- never read here: {counts['never']}",
        "",
        "## Burn-down",
        "",
    ]
    outstanding = [unit for unit, state in states.items() if state != "covered"]
    if not outstanding:
        lines.append(
            "Every carried canonical unit is covered by a recorded pass at its current text."
        )
    else:
        by_file: dict[str, list[str]] = {}
        for unit in outstanding:
            path = unit.split(SECTION_DELIM, 1)[0]
            by_file.setdefault(path, []).append(unit)
        for path in sorted(by_file):
            lines.append(f"### {path}")
            lines.append("")
            for unit in sorted(by_file[path]):
                section = (
                    unit.split(SECTION_DELIM, 1)[1] if SECTION_DELIM in unit else "(whole file)"
                )
                lines.append(f"- **{section}** - {states[unit]}")
            lines.append("")
    orphans = sorted(set(ledger) - set(current))
    if orphans:
        lines.extend(
            [
                "## Passes with no unit",
                "",
                (
                    "Recorded against a unit this tree no longer holds, so the section was renamed or"
                    " removed after the pass."
                ),
                "",
            ]
        )
        lines.extend(f"- {unit}" for unit in orphans)
        lines.append("")
    if absent:
        lines.extend(
            [
                "## Declared but not held here",
                "",
                (
                    "A manifest path this hub does not itself carry, which is ordinary for one scoped to"
                    " a project type this hub is not, and for a section the manifest names that the"
                    " file does not hold."
                ),
                "",
            ]
        )
        lines.extend(f"- {path}" for path in absent)
        lines.append("")
    lines.extend(
        [
            "[ledger]: ./canonical-review.json",
            "[issue]: https://github.com/ptr727/ProjectTemplate/issues/1138",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(root: Path) -> int:
    """Rewrite the burn-down from the ledger, returning how many units it covers.

    Called by `record` as well as by `report`, because a recorded pass that left the committed
    counts describing the previous state would put a stale burn-down in front of every reader of
    it, and the one procedure that writes the ledger is the one place that knows to.
    """
    current, absent = units(root)
    text = render_report(current, read_ledger(root), absent)
    path = root / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return len(current)


def cmd_report(args: argparse.Namespace) -> int:
    root = local_review.repo_root()
    if not args.check:
        emit(f"wrote {REPORT} over {write_report(root)} unit(s).")
        return EXIT_COVERED
    # Read-only, on `build_dist.py --check`'s contract, because a burn-down nothing verifies is current only by accident.
    # Deleting a unit outright changes no recorded pass, so `check` stays covered while the committed report still counts and lists the unit that is gone.
    current, absent = units(root)
    want = render_report(current, read_ledger(root), absent)
    path = root / REPORT
    have = path.read_bytes().decode("utf-8") if path.is_file() else None
    if have == want:
        emit(f"{REPORT} is current over {len(current)} unit(s).")
        return EXIT_COVERED
    emit(
        f"{REPORT} does not describe the ledger and the tree."
        f"\nRun: python3 scripts/canonical_review.py report",
        sys.stderr,
    )
    return EXIT_NOT_COVERED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="every carried canonical unit and its digest, as JSON")
    p_list.set_defaults(handler=cmd_list)

    p_status = sub.add_parser("status", help="what is covered, stale, or never read here, as JSON")
    p_status.set_defaults(handler=cmd_status)

    p_check = sub.add_parser(
        "check", help="exit 0 covered, 1 a changed unit is uncovered, 2 could not run"
    )
    p_check.set_defaults(handler=cmd_check)

    p_record = sub.add_parser("record", help="record a full-content pass over one or more units")
    p_record.add_argument(
        "--reviewer", required=True, help=f"one of: {', '.join(sorted(REVIEWERS))}"
    )
    p_record.add_argument(
        "--unit",
        required=True,
        action="append",
        metavar="KEY=DIGEST",
        help="a unit key and the digest the reviewer read, repeatable",
    )
    p_record.add_argument("--findings", type=int, default=None, help="how many findings it raised")
    p_record.add_argument(
        "--target",
        default=None,
        help=(
            f"target branch (default {DEFAULT_TARGET})"
            ", whose merge-base against this branch is stamped as hubCommit"
        ),
    )
    p_record.set_defaults(handler=cmd_record)

    p_report = sub.add_parser("report", help=f"write {REPORT}")
    p_report.add_argument(
        "--check",
        action="store_true",
        help="read-only: exit 0 where the report is current, 1 where it is not",
    )
    p_report.set_defaults(handler=cmd_report)

    p_check.add_argument(
        "--target",
        default=None,
        help=f"target branch (default {DEFAULT_TARGET}), resolved as origin/<value> first",
    )

    args = parser.parse_args(argv)
    code = EXIT_CANNOT_RUN
    try:
        code = int(args.handler(args))
        return code
    except CannotRun as exc:
        emit(f"canonical-review: {exc}", sys.stderr)
        return EXIT_CANNOT_RUN
    # A reader that closes early, `| head -1` being the ordinary case, otherwise raises here.
    # It then raises again during the interpreter's own shutdown flush, which exits 120, outside the contract.
    except BrokenPipeError:
        # A backstop only, since `emit` absorbs a closed reader at the point of writing.
        # Whatever verdict was reached is still the honest answer.
        local_review.silence(sys.stdout)
        local_review.silence(sys.stderr)
        return code
    # A crash is the check not having run, so it reports the boundary code.
    # Falling through to the interpreter's own exit 1 would read as the not-covered verdict, and a capture point folding that reports an execution boundary as a gate finding.
    # `blobs_at` is the reachable case: it runs git with a timeout and catches neither OSError nor TimeoutExpired, both of which a loaded host can produce.
    except Exception as exc:  # noqa: BLE001
        emit(f"canonical-review: unexpected failure ({type(exc).__name__}: {exc})", sys.stderr)
        return EXIT_CANNOT_RUN


if __name__ == "__main__":
    sys.exit(main())
