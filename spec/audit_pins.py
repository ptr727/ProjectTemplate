"""The audit's `pins.age` check: how far each caller's hub reusable-workflow pin trails the newest hub release.

A pin behind the newest release is an ADVISORY, since Dependabot moves it within days of a release.
A pin a newer release has superseded for longer than PIN_AGE_DAYS is DRIFT, since by then the update
mechanism has stalled. The age runs from the release that first superseded the pin, so a pin is never
late before a newer release exists. A registry driftNote naming `(pins.age)` holds such a pin back on
purpose, and the pin then reports as an ADVISORY. The note is stale once every pin's age reads and no
pin is behind the newest release.
"""

import functools
import posixpath
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

CHECK_ID = "pins.age"
PIN_AGE_DAYS = 30
WORKFLOW_DIR = ".github/workflows"

Finding = tuple[str, str]
Release = tuple[str, datetime, bool]


def workflow_paths(tree: Iterable[str]) -> list[str]:
    """The caller workflow files a tree carries, directly under `.github/workflows/`."""
    return sorted(
        p for p in tree if posixpath.dirname(p) == WORKFLOW_DIR and p.endswith((".yml", ".yaml"))
    )


def hub_pins(text: str, hub: str) -> dict[str, list[str]] | None:
    """`{ref: [task file, ...]}` for each job-level `uses:` reaching one of `hub`'s reusable workflows.

    `hub` is `<owner>/<repo>`, matched without regard to case, as GitHub matches it.
    A workflow that does not parse is None, since its pins are unread rather than absent.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return {}
    prefix = f"{hub}/{WORKFLOW_DIR}/".casefold()
    out: dict[str, list[str]] = {}
    for job in jobs.values():
        uses = job.get("uses") if isinstance(job, dict) else None
        if not isinstance(uses, str) or not uses.casefold().startswith(prefix) or "@" not in uses:
            continue
        target, ref = uses[len(prefix) :].rsplit("@", 1)
        out.setdefault(ref, [])
        if target not in out[ref]:
            out[ref].append(target)
    return out


def _published(value: str) -> datetime:
    return datetime.fromisoformat(value)


def version_key(tag: str) -> tuple[int, ...] | None:
    """A release tag's numeric version, ignoring any `-suffix`, or None for a tag of another shape.

    Releases are ordered by this rather than by publish time, since a re-run can publish an older
    version after a newer one.
    """
    parts = tag.split("-", 1)[0].split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def _listed(read: Callable[[str], Any], path: str) -> list[Any]:
    listed = read(path)
    if listed is None:
        raise RuntimeError(f"gh api {path}: returned an empty response")
    return listed


def _workflow_text(path: str, cached: str | None, read_text: Callable[[str], str | None]) -> str:
    text = cached or read_text(path)
    if text is None:
        raise RuntimeError(f"{path}: no inline content returned")
    return text


@functools.cache
def hub_releases(read: Callable[[str], Any], hub: str) -> tuple[dict[str, Release], dict[str, str]]:
    """`({tag: (tag, published, prerelease)}, {commit sha: tag})` for `hub`'s published releases.

    Read once per run through `read`, the audit's `gh` call. An empty response body raises, as the
    audit's own repository listing does, since it is a failed read rather than no release. A draft
    has no tag a pin can reach, so it is left out. An annotated tag resolves to its tag object rather than a commit, so a pin on
    its commit reads as naming no release.
    """
    releases: dict[str, Release] = {}
    page = 1
    while True:
        batch = _listed(read, f"repos/{hub}/releases?per_page=100&page={page}")
        for r in batch:
            if not r.get("draft") and r.get("published_at"):
                tag = r["tag_name"]
                releases[tag] = (tag, _published(r["published_at"]), bool(r.get("prerelease")))
        if len(batch) < 100:
            break
        page += 1
    refs = _listed(read, f"repos/{hub}/git/matching-refs/tags/")
    shas = {}
    for ref in refs:
        tag = ref["ref"].removeprefix("refs/tags/")
        if tag in releases and ref["object"]["type"] == "commit":
            shas[ref["object"]["sha"].casefold()] = tag
    return releases, shas


def pin_findings(
    entry: dict[str, Any],
    pins: Mapping[str, Mapping[str, list[str]] | None],
    releases: dict[str, Release],
    shas: dict[str, str],
    now: datetime,
) -> list[Finding]:
    """The findings for `pins`, `{workflow path: {ref: [task file, ...]}}`, against the hub's releases.

    A path mapped to None is a workflow that does not parse, so its pins are unread.
    """
    held = any(f"({CHECK_ID})" in n for n in entry.get("driftNotes", []))
    stable = [(v, r) for r in releases.values() if not r[2] and (v := version_key(r[0]))]
    newest = max(stable)[1][0] if stable else None
    bound = timedelta(days=PIN_AGE_DAYS)
    out: list[Finding] = []
    trailing = unread = False
    for path, refs in sorted(pins.items()):
        if refs is None:
            unread = True
            out.append(("ADVISORY", f"pins: {path} does not parse as YAML, so its pins are unread"))
            continue
        for ref, tasks in refs.items():
            named = f"{path} pins {', '.join(tasks)} at `{ref}`"
            pinned = releases.get(ref) or releases.get(shas.get(ref.casefold(), ""))
            version = version_key(pinned[0]) if pinned else None
            if pinned is None or version is None:
                unread = True
                out.append(
                    ("ADVISORY", f"pins: {named}, which is no hub release, so its age is unread")
                )
                continue
            newer = [r for v, r in stable if v > version]
            if not newer:
                continue
            trailing = True
            age = now - min(r[1] for r in newer)
            behind = f"{named} ({pinned[0]}), {len(newer)} release(s) behind {newest}"
            if age <= bound:
                out.append(("ADVISORY", f"pins: {behind}"))
                continue
            past = f"superseded {age.days} days ago, past the {PIN_AGE_DAYS}-day bound"
            if held:
                out.append(
                    ("ADVISORY", f"pins: {behind}, {past}, held by a driftNote ({CHECK_ID})")
                )
            else:
                out.append(
                    (
                        "DRIFT",
                        (
                            f"pins: {behind}, {past}. Merge the pending Dependabot bump or check "
                            f"why none opened, or record a driftNote naming ({CHECK_ID}) with the "
                            "reason the pin is held back."
                        ),
                    )
                )
    if held and not trailing and not unread:
        out.append(
            (
                "DRIFT",
                (
                    f"registry: a driftNote holds a pin back ({CHECK_ID}), but no pin is behind "
                    "the newest hub release. Delete the note."
                ),
            )
        )
    return out


def pin_age_findings(
    entry: dict[str, Any],
    hub: str,
    tree: Iterable[str] | None,
    read_text: Callable[[str], str | None],
    read: Callable[[str], Any],
    content: dict[str, str | None],
    now: datetime | None = None,
) -> list[Finding]:
    """The `pins.age` findings for one repository, reading the hub's releases only where it pins one.

    `read_text` reads a file at the audited commit and `read` is the audit's `gh` call. A file the
    baseline stage already read into `content` is taken from there instead.
    A failed read is one ERROR, so it never discards the repository's other findings. An unread
    tree reports nothing, since `tree.readable` reports it and a hold could not be judged stale.
    """
    if tree is None:
        return []
    pins: dict[str, dict[str, list[str]] | None] = {}
    try:
        for path in workflow_paths(tree):
            found = hub_pins(_workflow_text(path, content.get(path), read_text), hub)
            if found != {}:
                pins[path] = found
        releases, shas = hub_releases(read, hub) if any(pins.values()) else ({}, {})
    except RuntimeError as e:
        return [("ERROR", f"pins: could not read the caller pins or {hub}'s releases: {e}")]
    return pin_findings(entry, pins, releases, shas, now or datetime.now(UTC))
