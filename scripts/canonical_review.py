#!/usr/bin/env python3
"""Record and report full-content reviews of the canonical content this hub authors and other repos carry.

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

**The read is swept periodically rather than gated at a push.** Two weeks of fleet review rounds
measured the local passes a push owed as a large share of what a pull request spent, while what they
returned went unclassified, so the two were never weighed against each other and the call on that
evidence
(ptr727/ProjectTemplate#1631) was to sweep the read rather than gate it. Nothing here refuses a
push or a pull request any more.
`sweep` names the units whose text has moved past the pass that read them, and beside them a
bounded slice of the backlog #1138 records, newest-committed first so a unit just authored here is
read without waiting behind every older one. That is the work one scheduled run files and an agent
session performs, and `report` renders the whole backlog, to standard output rather than into the tree, for the reason
`scripts/README.md` gives (ptr727/ProjectTemplate#1268).

The verdict vocabulary is `scripts/local_review.py`'s, so a caller reading an exit code from
either engine need not know which one answered: 0 is covered, 1 is a finding, and 2 is the check
itself not having run.

Usage:
    python3 scripts/canonical_review.py list                 every unit and its digest, as JSON
    python3 scripts/canonical_review.py status               what is covered, stale, or never read
    python3 scripts/canonical_review.py sweep                the units it asks for this round
    python3 scripts/canonical_review.py record --reviewer agent-skill --unit '<key>=<digest>'
    python3 scripts/canonical_review.py report               render the burn-down to standard output
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
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
# Two engines under one rule that disagree about which branch "develop" means, or about what an exit code says, are a defect in the pair rather than in either.
CannotRun = local_review.CannotRun
DEFAULT_TARGET = local_review.DEFAULT_TARGET
EXIT_COVERED = local_review.EXIT_COVERED
EXIT_NOT_COVERED = local_review.EXIT_NOT_COVERED
EXIT_CANNOT_RUN = local_review.EXIT_CANNOT_RUN
emit = local_review.emit
git = local_review.git

MANIFEST = "spec/files.json"
LEDGER = "reports/canonical-review.json"
# The lock `record` holds around its read, merge, and write of the ledger, in this worktree's own git directory so a record killed mid-write leaves its lock where no add can stage it.
# `held_lock` appends `.lock` to the path it is given.
LEDGER_LOCK_NAME = "canonical-review-ledger"
LOCK_TIMEOUT = 10.0

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
SECTION_DELIM = build_dist.SECTION_DELIM
# The region before a document's first level-two heading is content a carrier reads like any other, so it is a unit rather than a gap between them.
# A literal `## (preamble)` heading would collide with it, which file_units then refuses as a duplicate rather than resolving, since only one of the two is a section.
PREAMBLE = "(preamble)"

# The generator's own source-to-distribution mapping, read from it rather than restated here.
# A downstream repository carries `.github/skills/`, and a defect found in one is fixed in `.agents/skills/`, which `scripts/build_dist.py --check` then holds the carried copy equal to.
# Keying the unit on the generated path would name a file no fix may edit.
GENERATED_SKILLS = build_dist.GITHUB_SKILLS.relative_to(build_dist.ROOT).as_posix()
AUTHORED_SKILLS = build_dist.SKILLS_SRC.relative_to(build_dist.ROOT).as_posix()

# How many never-read units one sweep asks for beside the stale ones.
# A bound rather than the whole backlog, since filing every unread unit as one week's work files a list nobody starts, and nothing at all leaves a newly authored unit with no reader, which is the case the rule is written about.
BACKLOG_SLICE = 5

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


def tracked_files(root: Path) -> set[str]:
    """Every path git tracks in this working tree, plus every untracked one it does not ignore.

    Git's view rather than the filesystem's, because they disagree in ways that matter here. A
    filesystem walk of a carried tree picks up whatever happens to be sitting in it, so a
    gitignored `.DS_Store` or an editor backup becomes canonical content: the first fails the
    UTF-8 decode and takes every subcommand to exit 2, and the second becomes a unit `sweep` names
    and `record` writes into the ledger forever. Neither is content any repository carries.
    """
    # Tracked plus untracked-and-not-ignored, rather than tracked alone.
    # A carried file this branch has created but not staged is content a carrier will receive, and dropping it would narrow the unit set exactly where a new canonical is added.
    # Ignored paths stay out, which is what keeps a stray .DS_Store or build artifact from becoming canonical content.
    listing = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", root=root)
    return {path for path in listing.split("\0") if path}


def select_carried(
    manifest: dict[str, Any], tracked: set[str]
) -> tuple[dict[str, list[str] | None], list[str]]:
    """What a manifest declares carried over `tracked`, and what it declares that is not there.

    Parameterized on the path set rather than reading the tree itself, so the membership question
    and the manifest question stay separable and a caller answering one of them differently does
    not have to reimplement the other.

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


def build_units(
    carried: dict[str, list[str] | None],
    read: Callable[[str], bytes | None],
) -> tuple[dict[str, str], list[str]]:
    """Unit key -> text for the carried paths `read` can supply, plus declared sections it lacks.

    A path the reader has nothing for contributes no units, a manifest entry scoped to a project
    type this repository is not being the ordinary case. A declared section the file does not hold
    is a different thing entirely, a manifest naming a heading that is not there, so it is reported
    rather than passed over.
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


def read_ledger(root: Path) -> dict[str, dict[str, Any]]:
    """The recorded passes, keyed by unit.

    An absent ledger is an empty record rather than a boundary, since the first repository to run
    this has nothing recorded yet and refusing there would make the record impossible to adopt. An
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


def ledger_lock(root: Path) -> Path:
    """The path `local_review.held_lock` guards the ledger under, in this worktree's git directory."""
    return local_review.git_dir(root) / LEDGER_LOCK_NAME


def write_ledger(root: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Rewrite the ledger as `entries`, one per unit, ordered by unit key.

    Sorted and one entry per unit so a concurrent branch touching a different unit merges cleanly,
    and so the diff of a recorded pass reads as the pass rather than as a reordering. The caller
    holds `ledger_lock` across the read this replaces, since two overlapping records would
    otherwise each read a ledger without the other's pass and the second write would drop it.
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
    """The target name and the merge-base commit a recorded pass is stamped with.

    A merge-base rather than this branch's own tip, since a squash merge discards that tip and an
    amend moves it, so a stamp anchored there resolves nowhere once the branch is gone. The
    merge-base is ordinarily a commit the target's remote-tracking ref already holds.
    """
    name = local_review.resolve_target(target)
    return name, local_review.merge_base(name, root)


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
    # Reports rather than gates, so a caller under `set -e` can run it whatever the coverage is.
    # A boundary still exits 2 from `main`, which is what puts this in a local gate block at all.
    return EXIT_COVERED


def newest_commit_times(root: Path, rels: list[str]) -> dict[str, float]:
    """Each path's last commit time, which is what orders the never-read backlog.

    Newest first rather than oldest, because a unit this repository has just authored is the one a
    carrier is about to receive unread, and that is the case the whole rule is written about. A unit
    unread for months can wait. The date read is the unit's own file rather than `spec/files.json`,
    so widening the manifest on its own lifts nothing, while declaring a section in the commit that
    writes it lifts that unit like any other.

    Three answers rather than two. A commit dates the path. A path git tracks that no commit holds
    is a file this branch has created and not committed, which `tracked_files` deliberately counts
    as a unit, and it is the newest content there is rather than an undatable one, so it sorts
    first. Anything else sorts last, which is a git failure in the ordinary case and every path in
    a repository with no commits at all, where git exits 128 rather than answering. The order is a
    priority rather than a claim about the content, so a uniform last is harmless there.
    """
    times: dict[str, float] = {}
    for rel in rels:
        try:
            # --literal-pathspecs, since a path holding a glob character would otherwise be matched as a pattern and dated from another file, and one opening with a colon would parse as pathspec magic.
            stamp = git(
                "--literal-pathspecs", "log", "-1", "--format=%ct", "--", rel, root=root
            ).strip()
        except CannotRun:
            times[rel] = 0.0
            continue
        # Empty stdout on a zero exit is the uncommitted case, since git found the path and no commit naming it.
        times[rel] = float(stamp) if stamp else float("inf")
    return times


def backlog_slice(root: Path, never: list[str]) -> list[str]:
    """The never-read units this sweep asks for, newest-committed first and bounded."""
    if not never:
        return []
    times = newest_commit_times(root, sorted({unit.split(SECTION_DELIM, 1)[0] for unit in never}))
    # The key breaks a tie, so two units in one file come out in a stable order and one tree state always renders one list.
    ordered = sorted(never, key=lambda unit: (-times[unit.split(SECTION_DELIM, 1)[0]], unit))
    return ordered[:BACKLOG_SLICE]


def code_span(text: str) -> str:
    """A non-empty `text` as a Markdown code span, whatever backticks it holds.

    A unit key is a heading, and a heading may name a command, so a key holding a backtick is
    ordinary rather than exotic: one in this tree does. Wrapped in single backticks it splits into
    two spans with the middle rendered as prose, which puts the path and the digest in different
    spans and makes the `record` argument under it uncopyable. CommonMark's own rule is a fence
    longer than any run inside, padded with a space where the content touches a backtick.

    Non-empty because empty text would render as two literal backticks rather than a span, and the
    pad reads backticks alone, so text bounded by spaces would lose one at each end. A unit key is
    a heading or a tracked path, both single-line and stripped, so neither shape reaches here.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def render_sweep(
    root: Path, current: dict[str, str], ledger: dict[str, dict[str, Any]]
) -> tuple[str, int]:
    """The sweep's work list and how many units are on it, as Markdown for an issue body.

    Two lists rather than one. A stale unit is text a carrier is receiving now that no pass here
    has read, and every one of those is asked for. A never-read unit is the backlog
    ptr727/ProjectTemplate#1138 records, and a bounded slice of it is asked for as well, because
    a unit nothing has ever read here includes the one this repository authored last week.
    """
    states = {unit: state_of(unit, value, ledger) for unit, value in current.items()}
    stale = sorted(unit for unit, state in states.items() if state == "stale")
    never = sorted(unit for unit, state in states.items() if state == "never")
    fresh = backlog_slice(root, never)
    lines = [
        "# Canonical content review sweep",
        "",
        (
            "Filed by `.github/workflows/canonical-review-sweep.yml` from"
            " `python3 scripts/canonical_review.py sweep`. Every unit below is text a repository"
            " carrying this content receives without a pass here having read it whole. The"
            ' `local-strict-review` Skill\'s "The Carried-Content Sweep" says how each pass is run'
            " and what its findings are owed."
        ),
        "",
        f"## {len(stale)} unit(s) whose text moved past its pass",
        "",
    ]
    # The key and the whole digest, in the shape `record` takes them, so working the issue is a copy rather than a second lookup against `list` over every unit in the tree.
    # A digest the tree has moved past since refuses the record, which is the content having moved rather than a fault in the list, and the answer is a read at the unit's current text.
    lines.extend("- " + code_span(f"{unit}={current[unit]}") for unit in stale)
    lines.extend(["", f"## {len(fresh)} unit(s) from the never-read backlog", ""])
    # The paragraph describes how a slice is chosen, so it is emitted only where there is one.
    # Rendered unconditionally it outlives its own subject: once the backlog is worked off, every issue and every job summary would carry a description of units the document does not hold.
    if fresh:
        lines.extend(
            [
                (
                    f"No pass here has ever read these. A sweep takes up to {BACKLOG_SLICE},"
                    " ordered by how recently the file each one sits in was last committed, so"
                    " recently authored content comes ahead of text that has sat unread for months"
                    " and the backlog shrinks by that many a round rather than waiting on a reader"
                    " who volunteers. A carried file no commit holds yet leads, being newer than any"
                    " of them. The key is the file rather than the unit, so committing to a file"
                    " lifts every unread unit in it, including the sections that commit never"
                    " touched."
                ),
                "",
            ]
        )
    lines.extend("- " + code_span(f"{unit}={current[unit]}") for unit in fresh)
    if stale or fresh:
        lines.extend(
            [
                "",
                "Record each pass at the digest above, which is the text that was read:",
                "",
                "```sh",
                "python3 scripts/canonical_review.py record --reviewer agent-skill --unit '<key>=<digest>'",
                "```",
            ]
        )
    lines.extend(
        [
            "",
            (
                f"{len(current)} units carried, {len(never)} of them never read here, which"
                " `python3 scripts/canonical_review.py report` renders in full."
            ),
            "",
        ]
    )
    # A pass recorded against a unit the tree no longer holds, which a renamed or deleted section produces.
    # Reported here rather than left to `status` alone, because no pass closes an orphan: the new key is read under the backlog above, and the old one stays recorded against a unit the tree no longer holds.
    # Not counted as work, since deciding that a section is gone rather than moved is a reader's call and no pass closes it.
    orphans = sorted(set(ledger) - set(current))
    if orphans:
        lines.extend(
            [
                f"## {len(orphans)} pass(es) with no unit",
                "",
                (
                    "Recorded against a unit this tree no longer holds, so the section was renamed"
                    " or removed after the pass. A renamed one is read again under its new key,"
                    " which joins the never-read backlog above rather than this list."
                ),
                "",
            ]
        )
        lines.extend("- " + code_span(unit) for unit in orphans)
        lines.append("")
    return "\n".join(lines), len(stale) + len(fresh)


def cmd_sweep(args: argparse.Namespace) -> int:
    """Render the work list, and report in the exit code whether it holds anything."""
    root = local_review.repo_root()
    current, _ = units(root)
    body, count = render_sweep(root, current, read_ledger(root))
    emit(body)
    # 1 rather than 0 where the list holds anything, which is how the workflow tells a sweep with work from one without under `set -Eeuo pipefail`.
    return EXIT_NOT_COVERED if count else EXIT_COVERED


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
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Stamped as the merge-base against the target, not HEAD, since a squash merge discards HEAD and an amend moves it.
    _, base = resolve_base(args.target, root)
    # The same lock as `local_review.write_pass`, for the same reason: a record is a read, a merge, and a write, and two of them interleaved lose the pass the second one never read.
    lock = ledger_lock(root)
    fd_lock = local_review.held_lock(lock, LOCK_TIMEOUT)
    try:
        ledger = read_ledger(root)
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
    finally:
        os.close(fd_lock)
        Path(str(lock) + ".lock").unlink(missing_ok=True)
    emit(f"recorded {args.reviewer} over {len(wanted)} unit(s) in {LEDGER}.")
    return EXIT_COVERED


def render_report(
    current: dict[str, str], ledger: dict[str, dict[str, Any]], absent: list[str]
) -> str:
    """The burn-down, grouped by the file a unit belongs to, as Markdown for a job summary or a pager."""
    states = {unit: state_of(unit, value, ledger) for unit, value in current.items()}
    counts = {
        state: sum(1 for s in states.values() if s == state)
        for state in ("covered", "stale", "never")
    }
    lines = [
        "# Canonical content review coverage",
        "",
        (
            "Rendered by `python3 scripts/canonical_review.py report` from"
            " `reports/canonical-review.json`, which `canonical_review.py record` writes. The"
            " ledger is tracked and this rendering is not, so it describes the tree it was"
            " rendered from and nothing older."
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
    lines.extend(["[issue]: https://github.com/ptr727/ProjectTemplate/issues/1138", ""])
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    """Render the burn-down to standard output, writing nothing into the tree, per `scripts/README.md`."""
    root = local_review.repo_root()
    current, absent = units(root)
    emit(render_report(current, read_ledger(root), absent))
    return EXIT_COVERED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="every carried canonical unit and its digest, as JSON")
    p_list.set_defaults(handler=cmd_list)

    p_status = sub.add_parser("status", help="what is covered, stale, or never read here, as JSON")
    p_status.set_defaults(handler=cmd_status)

    p_sweep = sub.add_parser(
        "sweep", help="exit 0 nothing owed, 1 a unit owes a read, 2 could not run"
    )
    p_sweep.set_defaults(handler=cmd_sweep)

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

    p_report = sub.add_parser(
        "report", help="render the burn-down from the ledger to standard output, as Markdown"
    )
    p_report.set_defaults(handler=cmd_report)

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
    # A last resort rather than a path with a known caller, since every git call goes through local_review.git and reports an OSError or a timeout as CannotRun above.
    # Reached in the tests by raising through `units`, because a handler nothing exercises is one nobody knows still folds the code it promises.
    except Exception as exc:  # noqa: BLE001
        emit(f"canonical-review: unexpected failure ({type(exc).__name__}: {exc})", sys.stderr)
        return EXIT_CANNOT_RUN


if __name__ == "__main__":
    sys.exit(main())
