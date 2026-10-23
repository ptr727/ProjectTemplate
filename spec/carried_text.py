"""The region reading the fidelity tools share, so each one splits a document alike.

Standard library only, because `scripts/build_dist.py`, `scripts/carry.py`, and
`scripts/canonical_review.py` import it under a plain `python3`.
"""

import re

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


def normalize(text):
    """`text` with line endings, action pins, and job `needs:` lists neutralized, the drift governed elsewhere.

    This is not placeholder masking, see spec/fidelity-model.md "Normalization".
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ACTION_PIN.sub(r"\1@<pin>", text)
    return _JOB_NEEDS.sub(r"\1needs: <needs>", text)


def fence_step(line, marker, marker_len):
    """One line's effect on fence state `(marker, marker_len)`, as `(marker, marker_len, is_boundary)`.

    CommonMark fences: at most 3 leading spaces, a closer of the opener's character at least as long
    with nothing after it, and no backtick in a backtick fence's info string.
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


def _h2_text(line, marker, boundary):
    """The folded text of a `## ` heading outside a fence, or None for any other line."""
    stripped = line.strip()
    if boundary or marker is not None or not stripped.startswith("## "):
        return None
    return stripped[2:].strip().lower()


def extract_section(text, heading):
    """The normalized `## <heading>` region, heading line included, up to the next H2, or None.

    The heading is found by its folded text, and its exact bytes are part of the region, so a re-cased
    heading is found and still reads as drift.
    """
    want = heading.strip().lower()
    out, capturing, marker, marker_len = [], False, None, 0
    for ln in normalize(text).split("\n"):
        marker, marker_len, boundary = fence_step(ln, marker, marker_len)
        name = _h2_text(ln, marker, boundary)
        if name is not None:
            if capturing:
                break
            capturing = name == want
        if capturing:
            out.append(ln)
    return "\n".join(out) if capturing else None


def strip_sections(text, names, keep_pins=False):
    """`text` with each named `## <heading>` region removed by position, normalized unless `keep_pins`.

    Positional, so an identical passage elsewhere in the document survives.
    """
    want = {n.strip().lower() for n in names}
    out, dropping, marker, marker_len = [], False, None, 0
    base = text.replace("\r\n", "\n").replace("\r", "\n") if keep_pins else normalize(text)
    for ln in base.split("\n"):
        marker, marker_len, boundary = fence_step(ln, marker, marker_len)
        name = _h2_text(ln, marker, boundary)
        if name is not None:
            dropping = name in want
        if not dropping:
            out.append(ln)
    return "\n".join(out)
