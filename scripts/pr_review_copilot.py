#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py>=4"]
# ///
"""Read one reviewer's body into the small typed result the gate in `pr_review.py` decides on.

The reviewer's wording and layout change without notice and with no programmatic API, so every
spelling this reads lives in `reviewers/<name>.json` rather than here. A changed spelling is a
data edit plus one fixture. A part of a body this cannot place is a one-line note. One that can
hold a finding also carries its own text as an unplaced part. `read` never raises. A body it
trips on reads as no refusal, `unknown` coverage and verdict, and the whole body unplaced. A new
shape reaches the gate as text, never a crash.

The body is parsed with markdown-it-py. A code span, a fence, or an indented block is therefore a token
of its own and never reads as markup. Only Copilot's current overview format, the one carrying the
`ccr-overview-v2` marker, is read. A Copilot body in any other layout reads as one unplaced part.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from markdown_it import MarkdownIt
from markdown_it.token import Token

VOCABULARY = Path(__file__).resolve().parent / "reviewers"
REVIEWERS = ("copilot", "coderabbit", "qodo")
COVERAGE = ("full", "partial", "none", "unknown")
REFUSALS = ("none", "quota", "file_count", "error", "unknown")
VERDICTS = ("clean", "flagging", "unknown")
KINDS = ("open", "suppressed", "previously_missed")
# Every key a vocabulary file carries, and the shape each value takes.
# A test holds each file to this, so a key the reader uses cannot be missing from one.
KEYS = {
    "login": str,
    "vetted": bool,
    "format_marker": str,
    "comment_heading": str,
    "refusal_openings": list,
    "refusal_kinds": dict,
    "coverage_prose": list,
    "rate_limit_service": str,
    "rate_limit": list,
    "verdicts": dict,
    "headings": list,
    "sections": dict,
    "closed_badges": list,
    "labels": list,
    "finding_lists": list,
    "effort": str,
    "table_header": str,
    "table_findings": str,
    "table_finding_marker": str,
    "discussion_link": str,
}

# The machine-readable coverage marker the fleet's own review instructions ask the reviewer for.
FLEET_REVIEW = re.compile(
    r"<!--\s*fleet-review:\s*reviewed=(\d+)\s+changed=(\d+)\s+findings=(\d+)\s*-->", re.IGNORECASE
)
TAG = re.compile(r"<(/?)(details|summary)(?=[\s/>])[^>]*>", re.IGNORECASE)
ANY_TAG = re.compile(r"<[^<>]*>")
# A severity badge is a `picture` whose only text is its `img` alt, kept so a printed finding says it.
BADGE = re.compile(
    r"<picture\b(?:(?!</?picture\b).)*?\salt=(?:\"([^\"]*)\"|'([^']*)')(?:(?!</?picture\b).)*?"
    r"</picture>",
    re.DOTALL | re.IGNORECASE,
)
SPOT = re.compile(r"(?P<path>.+):L?(?P<start>\d+)(?:\s*[-\u2013]\s*L?(?P<end>\d+))?")
EXCUSED = ("ADDED", "MODIFIED", "DELETED")
# A count longer than this is an identifier rather than anything a review states.
COUNT_DIGITS = 4
FINGERPRINT_HEX = 12
EXCERPT_LINES = 12
EXCERPT_WIDTH = 160

PARSER = MarkdownIt("commonmark").enable("table")


class Section(NamedTuple):
    """One counted section: its kind, the count its label states, its title, and its text.

    `findings` is None where the label states a count this reader cannot believe.
    """

    kind: str
    findings: int | None
    title: str
    text: str


class Unplaced(NamedTuple):
    """One part of a body the reader cannot place: the note naming it, and the part's own text."""

    note: str
    text: str


class Item(NamedTuple):
    """One findings-list item: its text on one line, and each `path:line` range its code spans name."""

    text: str
    spans: tuple[tuple[str, int, int], ...]


class Reading(NamedTuple):
    """What one body says, in the fields the gate reads.

    `open` is the count of open findings the body states, None where it states none or one this
    reader cannot believe. `listed` is the larger of the items its findings lists hold and the
    file table's findings, since both name the same findings. A filled cell counts each finding
    marker it carries, and once where it carries none, so the table alone can still undercount.
    `listed_items` is each findings-list item, each printed with a mark wherever the digest
    prints its findings with no thread.
    `unthreaded` is how many findings it names that no thread on the pull request carries.
    `table` is the paths the body's file table names, None where it carries no table. `unplaced`
    is each part the reader cannot place, with its text, the subset of `notes` that can hold a
    finding.
    """

    coverage: str = "unknown"
    coverage_line: str = ""
    refusal: str = "unknown"
    verdict: str = "unknown"
    verdict_heading: str = ""
    headline: str = ""
    effort: str = "unknown"
    effort_source: str = "unknown"
    open: int | None = None
    entries: int = 0
    listed: int = 0
    listed_items: tuple[Item, ...] = ()
    unthreaded: int = 0
    sections: tuple[Section, ...] = ()
    table: tuple[str, ...] | None = None
    rate_limited: str | None = None
    marked: bool = False
    notes: tuple[str, ...] = ()
    unplaced: tuple[Unplaced, ...] = ()

    def held(self, kind: str) -> int | None:
        """The findings in sections of `kind`, by their stated counts, None where one is unbelievable."""
        return total(s.findings for s in self.sections if s.kind == kind)

    @property
    def counts(self) -> dict[str, int | None]:
        """The findings by section: threaded, suppressed, and previously missed."""
        return {
            "threaded": self.open or 0,
            "suppressed": self.held("suppressed"),
            "previously_missed": self.held("previously_missed"),
        }


def load(name: str) -> dict:
    """The vocabulary in `reviewers/<name>.json`."""
    return json.loads((VOCABULARY / f"{name}.json").read_text(encoding="utf-8"))


def compiled(vocabulary: dict) -> dict:
    """The vocabulary with every pattern compiled, case-insensitively, so a typo fails at import."""

    def each(patterns: list[str]) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in patterns]

    return {
        **vocabulary,
        "refusal_openings": each(vocabulary["refusal_openings"]),
        "refusal_kinds": {k: each(v) for k, v in vocabulary["refusal_kinds"].items()},
        "rate_limit": each(vocabulary["rate_limit"]),
        "coverage_prose": each(vocabulary["coverage_prose"]),
        "verdicts": {k: each(v) for k, v in vocabulary["verdicts"].items()},
        "headings": each(vocabulary["headings"]),
        "sections": {k: each(v) for k, v in vocabulary["sections"].items()},
        "closed_badges": each(vocabulary["closed_badges"]),
        "labels": each([re.escape(x) for x in vocabulary["labels"]]),
        "finding_lists": each([re.escape(x) for x in vocabulary["finding_lists"]]),
        "comment_heading": re.compile(vocabulary["comment_heading"] or r"(?!)", re.IGNORECASE),
        "effort": re.compile(vocabulary["effort"] or r"(?!)", re.IGNORECASE),
        "discussion_link": re.compile(vocabulary["discussion_link"] or r"(?!)"),
        "table_finding_marker": re.compile(vocabulary["table_finding_marker"] or r"(?!)"),
    }


VOCABULARIES = {name: compiled(load(name)) for name in REVIEWERS}
LOGINS = {v["login"]: name for name, v in VOCABULARIES.items()}


def label(text: str) -> str:
    """Text as the vocabulary matches it: tags, emphasis, and non-ASCII dropped, lower case."""
    text = ANY_TAG.sub(" ", text)
    text = "".join(c for c in text if c.isascii())
    text = re.sub(r"[*_`]", "", text)
    return " ".join(text.split()).lower()


def plain(text: str) -> str:
    """Raw source as a person reads it, badges reduced to their alt text and other tags dropped."""
    text = BADGE.sub(lambda m: f"[{m.group(1) or m.group(2)}]", text)
    text = ANY_TAG.sub("", text)
    return text


def matched(patterns: list[re.Pattern[str]], text: str) -> re.Match[str] | None:
    """The first pattern that matches the whole of `text`."""
    return next((m for p in patterns if (m := p.fullmatch(text))), None)


def total(counts: Iterable[int | None]) -> int | None:
    """The sum of `counts`, or None where any of them is a count the reader cannot believe."""
    found = list(counts)
    return None if None in found else sum(c or 0 for c in found)


def believed(digits: str) -> int | None:
    """`digits` as a count, or None where it is longer than COUNT_DIGITS."""
    return int(digits) if len(digits) <= COUNT_DIGITS else None


def count_state(got: str, of: str) -> str:
    """A reviewed-of-changed count as `full`, `partial`, or `unknown` where either is too long or `got` exceeds `of`."""
    reviewed, changed = believed(got), believed(of)
    if reviewed is None or changed is None or reviewed > changed:
        return "unknown"
    return "full" if reviewed == changed else "partial"


def stated(match: re.Match[str]) -> int | None:
    """The count a matched label states: its `n` group, zero for a spelled none, one without one.

    A count longer than COUNT_DIGITS is None, since reading it as zero would report a clean round.
    """
    value = match.groupdict().get("n")
    if value is None:
        return 1
    return believed(value) if value.isdigit() else 0


class _Details:
    """One `<details>` element as the token stream places it, in source lines."""

    def __init__(self, parent: _Details | None, start: int) -> None:
        self.parent = parent
        self.start = start
        self.summary: str | None = None
        self.body = start + 1
        self.end: int | None = None


def details(tokens: list[Token]) -> list[_Details]:
    """Every `<details>` element in the body, outermost first, with its summary and line range.

    A tag is read only from HTML tokens, so one quoted in a code span or a fence is text. A tag in
    an inline token is placed by its block's first line plus the line breaks before it.
    """
    stack: list[_Details] = []
    found: list[_Details] = []
    collecting: list[str] | None = None

    def feed(raw: str, line0: int) -> None:
        nonlocal collecting
        pos = 0
        for m in TAG.finditer(raw):
            line = line0 + raw.count("\n", 0, m.start())
            if collecting is not None:
                collecting.append(raw[pos : m.start()])
            closing, name = m.group(1), m.group(2).lower()
            if name == "details" and not closing:
                node = _Details(stack[-1] if stack else None, line)
                stack.append(node)
                found.append(node)
            elif name == "details":
                if stack:
                    stack.pop().end = line
            elif not closing:
                collecting = []
            else:
                if collecting is not None and stack and stack[-1].summary is None:
                    stack[-1].summary = "".join(collecting)
                    stack[-1].body = line + 1
                collecting = None
            pos = m.end()
        if collecting is not None:
            collecting.append(raw[pos:])

    for token in tokens:
        line0 = (token.map or [0])[0]
        if token.type == "html_block":
            feed(token.content, line0)
        elif token.type == "inline":
            breaks = 0
            for child in token.children or []:
                if child.type in ("softbreak", "hardbreak"):
                    breaks += 1
                if child.type == "html_inline":
                    feed(child.content, line0 + breaks)
                elif collecting is not None:
                    collecting.append(child.content or " ")
    return found


def inline_text(token: Token) -> str:
    """The text an inline token renders, code spans included, markup dropped."""
    parts = []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return "".join(parts)


def bare(raw: str) -> str:
    """Text with its format characters, such as a zero-width space, dropped and its ends stripped."""
    return "".join(c for c in raw if unicodedata.category(c) != "Cf").strip()


def paths(token: Token) -> list[str]:
    """The paths a file table cell names: its code spans, or its whole text where it has none."""
    spans = [c.content for c in token.children or [] if c.type == "code_inline"]
    named = spans or [inline_text(token)]
    return [cleaned for raw in named if (cleaned := bare(raw))]


def read(body: str, reviewer: str = "copilot", threads: tuple[str, ...] | None = None) -> Reading:
    """The reading of one body by `reviewer`'s vocabulary, never raising.

    `threads` is the database id of each thread's first comment on the pull request, which an
    open entry's link names. None counts every linked entry as threaded.
    """
    try:
        return _read(body or "", reviewer, threads)
    except Exception as exc:  # noqa: BLE001
        failed = f"the reader failed on this body ({type(exc).__name__})"
        part = Unplaced(failed, (body or "").replace("\r\n", "\n").strip())
        return Reading(refusal="none", coverage_line=failed, notes=(failed,), unplaced=(part,))


@functools.lru_cache(maxsize=512)
def _read(body: str, reviewer: str, threads: tuple[str, ...] | None) -> Reading:
    vocab = VOCABULARIES[reviewer]
    text = body.replace("\r\n", "\n")
    lines = text.split("\n")
    tokens = PARSER.parse(text)
    notes: list[str] = []
    unplaced: list[Unplaced] = []

    def unread(note: str, start: int, end: int) -> None:
        notes.append(note)
        unplaced.append(Unplaced(note, "\n".join(lines[start:end]).strip()))

    def count_of(match: re.Match[str], name: str) -> int | None:
        if (count := stated(match)) is None:
            notes.append(f"a finding count this reader cannot believe: `{name}`")
        return count

    html = [t.content for t in tokens if t.type == "html_block"] + [
        c.content
        for t in tokens
        if t.type == "inline"
        for c in t.children or []
        if c.type == "html_inline"
    ]
    marked = bool(vocab["format_marker"]) and any(
        re.search(rf"<!--\s*{re.escape(vocab['format_marker'])}\s*-->", h) for h in html
    )
    rate_limited = next(
        (
            m.groupdict().get("service") or vocab["rate_limit_service"]
            for p in vocab["rate_limit"]
            if (m := p.search(text))
        ),
        None,
    )

    refusal = "none"
    first = next((t for t in tokens if t.type == "inline"), None)
    if first is not None and any(p.search(first.content) for p in vocab["refusal_openings"]):
        refusal = next(
            (k for k, ps in vocab["refusal_kinds"].items() if any(p.search(text) for p in ps)),
            "unknown",
        )
        return Reading(refusal=refusal, coverage="none", marked=marked)

    coverage, coverage_line = "none", ""
    # Partial ranks worst, being the reading that blocks, so nothing beside it can hide it.
    rank = {"full": 0, "unknown": 1, "partial": 2}
    for h in html:
        for m in FLEET_REVIEW.finditer(h):
            state = count_state(m.group(1), m.group(2))
            if coverage == "none" or rank[state] > rank[coverage]:
                coverage, coverage_line = state, m.group(0)
    # A body in another layout is one note, rather than one note per part of it.
    vet = vocab["vetted"] and (marked or not vocab["format_marker"])
    if vocab["vetted"] and vocab["format_marker"]:
        if not text.strip():
            notes.append("the body is empty")
        elif not marked:
            unread(
                f"the body carries no `<!-- {vocab['format_marker']} -->` marker, the one format "
                "this reader reads",
                0,
                len(lines),
            )

    found = details(tokens)
    sections: list[Section] = []
    claimed: list[tuple[int, int]] = []
    opaque: list[tuple[int, int]] = []
    counted: list[tuple[int, int]] = []
    opens: list[int | None] = []
    entries = linked = 0
    for node in found:
        end = node.end if node.end is not None else len(lines)
        if any(a <= node.start < b for a, b in claimed):
            continue
        summary = node.summary or ""
        name = label(summary)
        kind, match = next(
            ((k, m) for k, ps in vocab["sections"].items() if (m := matched(ps, name))),
            ("", None),
        )
        if match is None:
            if vet and node.parent is None:
                opaque.append((node.start, end + 1))
                unread(
                    f"a collapsed section this reader has no spelling for: `{name}`",
                    node.start,
                    end + 1,
                )
            continue
        claimed.append((node.start, end + 1))
        # A section counting its entries, resolved ones included, holds findings rather than a statement.
        if kind != "vetted" or match.groupdict().get("n") is not None:
            counted.append((node.start, end + 1))
        if kind == "vetted" or any(p.search(summary) for p in vocab["closed_badges"]):
            continue
        count = count_of(match, name)
        content = "\n".join(
            ln.rstrip()
            for raw in lines[node.body : end]
            if (ln := plain(re.sub(r"^\s*(?:>\s?)+", "", raw))).strip()
        )
        sections.append(Section(kind, count, " ".join(plain(summary).split()), content))
        if kind == "open":
            opens.append(count)
            got, links = entries_in(tokens, node.body, end, found, vocab)
            entries += got
            linked += sum(
                1 for ids in links if ids and (threads is None or set(ids) & set(threads))
            )

    verdict, verdict_heading, headline = "unknown", "", ""
    effort, effort_source = "unknown", "unknown"
    table: list[str] | None = None
    listed = tabled = 0
    listed_items: list[Item] = []

    def inside(token: Token, ranges: list[tuple[int, int]] = claimed) -> bool:
        return any(a <= (token.map or [0])[0] < b for a, b in ranges)

    def placed(n: int, t: Token) -> bool:
        if t.type == "heading_open":
            return True
        if t.level:
            return False
        if t.type == "table_open":
            return bool(vocab["table_header"]) and (
                table_cells(tokens, n, vocab["table_header"]) is not None
            )
        if t.type == "html_block":
            return bool(FLEET_REVIEW.search(t.content))
        if t.type != "paragraph_open":
            return False
        said = " ".join(inline_text(tokens[n + 1]).split())
        return (
            is_label(tokens[n + 1])
            or bool(vocab["effort"].fullmatch(label(said)))
            or any(p.match(said) for p in vocab["coverage_prose"])
            or any(matched(ps, label(said)) for ps in vocab["sections"].values())
        )

    stops = sorted(
        {a for a, _ in claimed + opaque}
        | {(t.map or [0])[0] for n, t in enumerate(tokens) if placed(n, t)}
    )

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.type == "heading_open" and not inside(token) and not inside(token, opaque):
            name = label(tokens[i + 1].content)
            kind = next((k for k, ps in vocab["verdicts"].items() if matched(ps, name)), "")
            if kind and verdict == "unknown":
                verdict, verdict_heading = kind, name
                headline = headline_after(tokens, i + 3, lines, vocab["coverage_prose"])
            elif not kind and vet and not matched(vocab["headings"], name):
                unread(
                    f"a heading this reader has no spelling for: `{name}`", *span(tokens, i, stops)
                )
        elif token.type == "paragraph_open":
            inline = tokens[i + 1]
            name = label(inline_text(inline))
            effort_match = vocab["effort"].fullmatch(name)
            if effort_match:
                level = effort_match.group("default") or effort_match.group("level")
                effort = level.lower()
                effort_source = "default" if effort_match.group("default") else "explicit"
            elif not inside(token) and (
                found_kind := next(
                    ((k, m) for k, ps in vocab["sections"].items() if (m := matched(ps, name))),
                    None,
                )
            ):
                if found_kind[0] == "open":
                    opens.append(count_of(found_kind[1], name))
            elif (listing := matched(vocab["finding_lists"], name)) and (
                items := items_after(tokens, i + 3, lines)
            ):
                listed += len(items)
                listed_items += items
            elif (
                vet
                and is_label(inline)
                and (listing or not matched(vocab["labels"], name))
                and not inside(token, opaque)
            ):
                note = (
                    f"a findings list label with no list this reader reads: `{name}`"
                    if listing
                    else f"a label this reader has no spelling for: `{name}`"
                )
                if token.level or inside(token):
                    notes.append(note)
                else:
                    unread(note, *span(tokens, i, stops))
        elif (
            token.type == "table_open"
            and vocab["table_header"]
            and not token.level
            and not any(a <= (token.map or [0])[0] < b for a, b in counted)
        ):
            if (cells := table_cells(tokens, i, vocab["table_header"])) is not None:
                heads, rows = cells
                table = (table or []) + [p for row in rows if row for p in paths(row[0])]
                if vocab["table_findings"] in heads:
                    column = heads.index(vocab["table_findings"])
                    marker = vocab["table_finding_marker"]
                    texts = [inline_text(row[column]) for row in rows if len(row) > column]
                    tabled += sum(
                        max(1, len(marker.findall(text)))
                        for text in texts
                        if any(c.isalnum() for c in text)
                    )
        i += 1

    # A count stated in a top-level paragraph opening on it, never a list item, outside counted sections.
    # A sentence opening on the wording reads as a count too, and that false partial is the loud failure.
    for n, token in enumerate(tokens[1:], start=1):
        line = (token.map or [0])[0]
        opener = tokens[n - 1]
        if token.type != "inline" or opener.type != "paragraph_open" or opener.level:
            continue
        if any(a <= line < b for a, b in counted):
            continue
        parts = [
            c.content if c.type == "text" else " "
            for c in token.children or []
            if c.type in ("text", "softbreak", "hardbreak")
        ]
        said = " ".join("".join(parts).split())
        m = next((m for p in vocab["coverage_prose"] if (m := p.match(said))), None)
        if m is None:
            continue
        state = count_state(m.group("got"), m.group("of"))
        if state == "unknown":
            notes.append(f"a coverage count this reader cannot believe: `{said}`")
        if coverage == "none" or rank[state] > rank[coverage]:
            coverage, coverage_line = state, said

    open_count = total(opens) if opens else None
    listed = max(listed, tabled)
    named = max(open_count or 0, listed)
    return Reading(
        coverage=coverage,
        coverage_line=coverage_line,
        refusal=refusal,
        verdict=verdict,
        verdict_heading=verdict_heading,
        headline=headline,
        effort=effort,
        effort_source=effort_source,
        open=open_count,
        entries=entries,
        listed=listed,
        listed_items=tuple(listed_items),
        unthreaded=max(named - linked, 0),
        sections=tuple(sections),
        table=tuple(table) if table is not None else None,
        rate_limited=rate_limited,
        marked=marked,
        notes=tuple(dict.fromkeys(notes)),
        unplaced=tuple(dict.fromkeys(unplaced)),
    )


def span(tokens: list[Token], start: int, stops: list[int]) -> tuple[int, int]:
    """The lines from the block opening at `start` to the next stop, the part a note names."""
    first = (tokens[start].map or [0])[0]
    return first, next((s for s in stops if s > first), sys.maxsize)


def fingerprint(parts: list[Unplaced]) -> str:
    """A short stable name for a set of unplaced parts, over their text in sorted order."""
    digest = hashlib.sha256()
    for text in sorted({p.text for p in parts}):
        digest.update(text.encode("utf-8") + b"\0")
    return digest.hexdigest()[:FINGERPRINT_HEX]


def excerpt(text: str) -> list[str]:
    """The first lines of a part, indented and cut to a width, with a closing line for the rest."""
    kept = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    shown = [ln if len(ln) <= EXCERPT_WIDTH else ln[:EXCERPT_WIDTH] + "..." for ln in kept]
    cut = len(kept) - EXCERPT_LINES
    more = [f"... {cut} more lines in the body"] if cut > 0 else []
    return [f"      {ln}" for ln in shown[:EXCERPT_LINES] + more]


def entries_in(
    tokens: list[Token], start: int, end: int, found: list[_Details], vocab: dict
) -> tuple[int, list[list[str]]]:
    """The entries at a section's margin and the thread ids each one links.

    An entry is a list item at the shallowest level in the section, outside any `<details>`
    nested in it. A back-reference inside an entry or a collapsed list is therefore not one.
    """
    nested = [(d.start, d.end or end) for d in found if start <= d.start < end]
    items = [
        (n, t)
        for n, t in enumerate(tokens)
        if t.type == "list_item_open"
        and t.map
        and start <= t.map[0] < end
        and not any(a <= t.map[0] <= b for a, b in nested)
    ]
    if not items:
        return 0, []
    margin = min(t.level for _, t in items)
    links = []
    for n, t in items:
        if t.level != margin:
            continue
        ids = []
        for inner in tokens[n + 1 :]:
            if inner.type == "list_item_close" and inner.level == margin:
                break
            for child in inner.children or []:
                href = str(child.attrs.get("href", "")) if child.type == "link_open" else ""
                if m := vocab["discussion_link"].search(href):
                    ids.append(m.group("id"))
        links.append(ids)
    return len(links), links


def headline_after(
    tokens: list[Token], start: int, lines: list[str], counts: list[re.Pattern[str]]
) -> str:
    """The first prose paragraph under a verdict heading, a coverage count before it passed over.

    A label or a bold line ends the search, since what follows it is no headline.
    """
    while start < len(tokens) and tokens[start].type == "paragraph_open":
        inline = tokens[start + 1]
        if is_label(inline) or (inline.children and inline.children[0].type == "strong_open"):
            if not any(p.match(" ".join(inline_text(inline).split())) for p in counts):
                return ""
        elif not any(p.match(" ".join(inline_text(inline).split())) for p in counts):
            a, b = tokens[start].map or (0, 0)
            return " ".join(" ".join(lines[a:b]).split())
        start += 3
    return ""


def is_label(inline: Token) -> bool:
    """Whether a paragraph is a label line, a short text ending on a colon."""
    text = inline_text(inline).strip()
    return text.endswith(":") and len(text.split()) <= 5


def items_after(tokens: list[Token], start: int, lines: list[str]) -> list[Item]:
    """Each item of the bullet or numbered list opening at `start`, at its own margin.

    An item's text is its source on one line, quote and list markers dropped. Its spans are read
    from its code spans, so a path holding a space or a bracket still reads whole.
    """
    if start >= len(tokens) or tokens[start].type not in ("bullet_list_open", "ordered_list_open"):
        return []
    level = tokens[start].level + 1
    items: list[Item] = []
    spans: list[tuple[str, int, int]] = []
    for token in tokens[start + 1 :]:
        if token.type.endswith("_list_close") and token.level == level - 1:
            break
        if token.type == "list_item_open" and token.level == level:
            a, b = token.map or (0, 0)
            said = " ".join(plain(re.sub(r"^\s*(?:>\s?)+", "", ln)) for ln in lines[a:b])
            text = re.sub(r"^(?:[-*+]|\d+[.)])\s*", "", " ".join(said.split()))
            spans = []
            items.append(Item(text, ()))
        for child in token.children or []:
            if child.type == "code_inline" and (m := SPOT.fullmatch(bare(child.content))):
                first = int(m.group("start"))
                spans.append((m.group("path"), first, max(first, int(m.group("end") or first))))
                items[-1] = Item(items[-1].text, tuple(spans))
    return items


def listed_lines(
    items: tuple[Item, ...], threads: list[dict], files: list[dict], cut: bool
) -> tuple[int, list[str]]:
    """How many items name a `path:line` no read thread sits on, and each item marked by that.

    A span counts only where its path is a changed file or a thread's, so `node:20` is no line.
    A mark is a pointer rather than a match, since a thread on the same line can hold another
    finding. An outdated thread's lines predate the head, so one on an item's path says only
    that. Where `cut`, the thread page was cut, so an item no read thread sits on is unknown.
    """
    firsts = [((t.get("comments") or {}).get("nodes") or [{}])[0] for t in threads]
    spots = {
        (c["path"], c.get("startLine") or c["line"], c["line"])
        for c in firsts
        if c.get("path") and isinstance(c.get("line"), int)
    }
    outdated = {c["path"] for c in firsts if c.get("path") and c.get("line") is None}
    known = {n.get("path") for n in files} | {c.get("path") for c in firsts}
    out = []
    unmatched = 0
    for item in items:
        named = [(p, a, b) for p, a, b in item.spans if p in known]
        if not named:
            mark = "no path:line code span"
        elif any(p == q and a <= d and c <= b for p, a, b in named for q, c, d in spots):
            mark = "thread at its line"
        elif any(p in outdated for p, _, _ in named):
            mark = "outdated thread on its path"
        else:
            mark = "thread page cut, unknown" if cut else "no thread at its line"
            unmatched += 1
        out.append(f"    [{mark}] {item.text}")
    return unmatched, out


def row_names(entry: str, path: str) -> bool:
    """Whether a file table row names `path`, exactly or by a glob.

    A glob matches one segment at a time, `*` and `?` never crossing a `/`, and every other
    character is literal. A row of `src/*` therefore names `src/a.py` and not `src/sub/b.py`.
    """
    if entry == path:
        return True
    want, have = entry.split("/"), path.split("/")
    return len(want) == len(have) and all(
        re.fullmatch(re.escape(w).replace(r"\*", "[^/]*").replace(r"\?", "[^/]"), h)
        for w, h in zip(want, have, strict=True)
    )


def left_out(named: tuple[str, ...], files: list[dict]) -> set[str]:
    """The changed files a file table may leave out: each one GitHub counts no changed line in.

    Such a file is added, modified, or deleted with no added and no deleted line, a binary file
    among them. An empty file, a mode-only change, and a text file `.gitattributes` marks binary
    read the same, since the file list holds nothing telling them apart. A rename or copy is
    never one, and nor is a file missing a count. None is left out unless a file with an added or
    deleted line remains to be named.
    """
    lineless = {
        n["path"]
        for n in files
        if n.get("path")
        and n.get("changeType") in EXCUSED
        and n.get("additions") == 0
        and n.get("deletions") == 0
    }
    if not any((n.get("additions") or 0) + (n.get("deletions") or 0) > 0 for n in files):
        return set()
    return {p for p in lineless if not any(row_names(e, p) for e in named)}


def table_cells(
    tokens: list[Token], start: int, header: str
) -> tuple[list[str], list[list[Token]]] | None:
    """The header labels and each body row's cells of the table opening at `start`.

    None where its first header is not `header`, since only the file table is read.
    """
    heads: list[str] = []
    rows: list[list[Token]] = []
    head = True
    for token in tokens[start + 1 :]:
        if token.type == "table_close":
            break
        if token.type == "tbody_open":
            head = False
        elif token.type == "tr_open" and not head:
            rows.append([])
        elif token.type == "inline" and head:
            heads.append(label(inline_text(token)))
        elif token.type == "inline" and rows:
            rows[-1].append(token)
    return (heads, rows) if heads and heads[0] == header else None


def html_lines(body: str) -> list[str]:
    """Each line of raw HTML in the body, stripped, so a line quoted in code is never one."""
    return [
        line.strip()
        for token in PARSER.parse((body or "").replace("\r\n", "\n"))
        if token.type == "html_block"
        for line in token.content.splitlines()
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pr_review_copilot.py",
        description="Print the reading of the review body on standard input.",
    )
    ap.add_argument("reviewer", nargs="?", default="copilot", choices=REVIEWERS)
    a = ap.parse_args(argv)
    # Read as bytes and decoded as UTF-8, since a Windows console's code page cannot decode a body.
    body = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    print(json.dumps(read(body, a.reviewer)._asdict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
