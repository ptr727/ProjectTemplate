"""The text reading the fidelity tools share, so each one splits a document at the same boundaries.

Fences and headings come from markdown-it-py. So every tool that imports this module runs under
`uv run --no-project --script`, or imports it only on the path that reads a region.
"""

import functools
import re
from typing import NamedTuple

import markdown_it

# A `uses: <action>@<40-hex sha>` pin plus an optional version-shaped comment, which Dependabot bumps per repo.
_ACTION_PIN = re.compile(r"(\buses:[ \t]*[^\s@]+)@[0-9a-fA-F]{40}(?:[ \t]+#[ \t]*v?[0-9][\w.\-]*)?")
# A job's `needs:` list, inline, scalar, or block, which a repo prunes to the targets it vendors.
_JOB_NEEDS = re.compile(
    r"(^[ \t]*)needs:[ \t]*"
    r"(?:\[[^\]\n]*\]"
    r"|[A-Za-z0-9_.\-]+[ \t]*(?=\n|$)"
    r"|(?:\n[ \t]+-[ \t]*[A-Za-z0-9_.\-]+[ \t]*)+)",
    re.MULTILINE,
)

# Block rules only, since a region boundary is a block fact, and skipping the inline pass keeps a parse per file cheap.
# Tables are on because GitHub renders them, and a table changes where a block ends.
_BLOCKS = markdown_it.MarkdownIt("commonmark").enable("table").disable("inline")


@functools.lru_cache(maxsize=16)
def _tokens(text):
    """The block tokens of `text`, held because one caller reads a document's headings and its code blocks both."""
    return _BLOCKS.parse(text)


class Heading(NamedTuple):
    """An ATX heading: its zero-based line, its level, and its parsed text."""

    line: int
    level: int
    text: str


class CodeBlock(NamedTuple):
    """A fenced or indented code block as the `[start, end)` lines it spans, and whether a container holds it."""

    start: int
    end: int
    fenced: bool
    closed: bool
    nested: bool


def normalize(text):
    """`text` with line endings, action pins, and job `needs:` lists neutralized, the drift governed elsewhere.

    This is not placeholder masking, see spec/fidelity-model.md "Normalization".
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ACTION_PIN.sub(r"\1@<pin>", text)
    return _JOB_NEEDS.sub(r"\1needs: <needs>", text)


def headings(text):
    """Every ATX heading at the top level of `text`, in order.

    A setext heading is left out, since every region this reads is opened by a `#` line. A heading
    inside a code block is content, which is why a parser reads this at all. So is one quoted in a
    blockquote or nested in a list item, since neither opens a section of the document.
    """
    tokens = _tokens(text)
    return [
        Heading(t.map[0], int(t.tag[1]), tokens[i + 1].content.strip())
        for i, t in enumerate(tokens)
        if t.type == "heading_open" and t.markup.startswith("#") and t.map and t.level == 0
    ]


def code_blocks(text):
    """Every fenced and indented code block in `text`, in order, at any nesting depth.

    A fence the parser ends with its container, or with the document, rather than at a closing line,
    reads as not `closed`. Only a top-level one runs on into whatever follows it once copied.
    """
    out = []
    for t in _tokens(text):
        if t.type not in ("fence", "code_block") or not t.map:
            continue
        start, end = t.map
        fenced = t.type == "fence"
        # A closed fence spans its opener, its content, and its closer, so one line is left over.
        # A final content line with no newline after it still counts as a line.
        content = t.content.count("\n") + (1 if t.content and not t.content.endswith("\n") else 0)
        closed = not fenced or end - start - 1 - content == 1
        out.append(CodeBlock(start, end, fenced, closed, t.level > 0))
    return out


def code_lines(text):
    """The zero-based lines of `text` that sit inside a code block, where a marker is content."""
    return frozenset(line for block in code_blocks(text) for line in range(block.start, block.end))


def sections(text):
    """`(heading, end)` for each level-two region of `text`, in order, the one place a region's extent is decided.

    The heading line is inside the region and the next level-two heading ends it. `end` is that
    heading's line, or None for a region that runs to the end of the document, so `lines[h.line:end]`
    slices the region either way.
    """
    starts = [h for h in headings(text) if h.level == 2]
    return [(h, starts[i + 1].line if i + 1 < len(starts) else None) for i, h in enumerate(starts)]


def section_span(text, heading):
    """The `[start, end)` lines of the first `## <heading>` region, or None when `text` has none.

    `end` is None for a region that runs to the end of the document. The match is on the parsed
    heading text, case-folded, so a re-cased heading is found rather than read as absent.
    """
    want = heading.strip().lower()
    return next(((h.line, end) for h, end in sections(text) if h.text.lower() == want), None)


def extract_section(text, heading):
    """The normalized `## <heading>` region, heading line included, up to the next H2, or None.

    The heading is found by its folded text. Its exact bytes are part of the region, so a re-cased
    heading is found and still reads as drift.
    """
    base = normalize(text)
    span = section_span(base, heading)
    if span is None:
        return None
    return "\n".join(base.split("\n")[span[0] : span[1]])


def strip_sections(text, names, keep_pins=False):
    """`text` with each named `## <heading>` region removed by position, normalized unless `keep_pins`.

    Positional, so an identical passage elsewhere in the document survives.
    """
    want = {n.strip().lower() for n in names}
    base = text.replace("\r\n", "\n").replace("\r", "\n") if keep_pins else normalize(text)
    lines = base.split("\n")
    dropped = set()
    for h, end in sections(base):
        if h.text.lower() in want:
            dropped.update(range(h.line, len(lines) if end is None else end))
    return "\n".join(ln for i, ln in enumerate(lines) if i not in dropped)
