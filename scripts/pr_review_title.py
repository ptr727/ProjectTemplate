"""A pull request title read against the fleet's title contract, for `status`, `wait`, and `merge`.

This is a library module and has no command line, and nothing here makes a call. It reads the
title off a payload `pr_review.py` already fetched. Its importers are `pr_review_answers.py`, which
holds the gate's exit and `merge`'s refusal, and `pr_review_digest.py`, which prints the field and
the fixes.

The contract is `comment-and-doc-style` "PR titles and commit messages". A squash merge into
`develop` takes the title as the commit subject, so the title becomes history at the merge. This
reads only the parts of that contract a pattern decides, which are these:

- The title is 72 characters or fewer.
- It has no trailing period.
- It is in title case, lowercasing only the bind words, whose list is exhaustive.

Imperative mood, a vague title, and a release-bump magnitude stay with review, since no pattern
decides them. Dependabot's titles are exempt, as the contract says.

Title case is read one word at a time, and a word is exempt where a pattern cannot tell its casing
is wrong. One is a word inside a code span. Another carries a character no English word carries,
such as a digit, a dot, a slash, or an underscore. A third has an upper-case letter after its
first, such as `macOS`. So `net8.0`, `softprops/action-gh-release`, and `README` pass as
written. A lower-case tool name such as `ruff` goes in a code span. A word joined to a code span,
such as the `'s` after one, is part of the name and skipped too. A hyphenated word capitalizes its
first part, and each later part unless it is a bind word, so `Built-in`, `24-Hour`, and
`On-Demand` pass. The first word is
capitalized whatever it is. A bind word opening a subtitle after a colon takes either case.
Punctuation around a word, such as a closing period or comma, is not part of it.
"""

from __future__ import annotations

import re

from pr_review_promotion import DEPENDABOT
from pr_review_rounds import is_open

MAX_LENGTH = 72
BIND_WORDS = frozenset(
    ("a", "an", "the", "and", "but", "or", "of", "in", "on", "at", "to", "by", "for", "from")
    + ("with", "as", "via", "into", "over", "per")
)
CODE_SPAN = re.compile(r"`[^`]*`")
SPAN = "\0"
EDGE = "\"'()[]{}<>,.;:!?*"
WORDLIKE = re.compile(r"^[^\W\d_]+(?:['\u2019][^\W\d_]+)*(?:-[^\W\d_]*(?:['\u2019][^\W\d_]+)*)*$")
OK, HELD, EXEMPT = "ok", "HELD", "exempt"
STATUS = "TITLE_NOT_CONVENTIONAL"


def mixed_case(part: str) -> bool:
    """Whether an upper-case letter follows the first, the mark of a name such as `macOS`."""
    return any(c.isupper() for c in part[1:])


def wanted(part: str, index: int, parts: int, first: bool, opens: bool) -> bool | None:
    """Whether a word part's first letter is owed upper case, or None where either case reads.

    `index` is the part's place in a hyphenated word of `parts` parts. `first` marks the word
    opening the title, and `opens` one opening a subtitle after a colon. There a bind word takes
    either case.
    """
    bind = part.lower() in BIND_WORDS
    if index == 0 and (first or parts > 1):
        return True
    if index == 0 and opens and bind:
        return None
    return not bind


def word_fix(word: str, first: bool, opens: bool) -> str:
    """The case fix one word owes, or empty, naming the word as it should read."""
    core = word.strip(EDGE)
    if not WORDLIKE.match(core):
        return ""
    parts, raised = core.split("-"), False
    for i, part in enumerate(parts):
        upper = wanted(part, i, len(parts), first, opens) if part and not mixed_case(part) else None
        if upper is None or part[0].isupper() == upper:
            continue
        raised = raised or upper
        parts[i] = (part[0].upper() if upper else part[0].lower()) + part[1:]
    fixed = "-".join(parts)
    if fixed == core:
        return ""
    if raised:
        return (
            f"write `{core}` as `{fixed}`, or put it in a code span where it is a name that "
            "keeps its own casing"
        )
    return f"write `{core}` as `{fixed}`, since a bind word stays lower case"


def problems(title: str) -> list[str]:
    """Each mechanical rule `title` breaks, phrased as the fix that clears it, else empty."""
    found = []
    if len(title) > MAX_LENGTH:
        found.append(f"shorten it from {len(title)} to {MAX_LENGTH} characters or fewer")
    if title.rstrip().endswith("."):
        found.append("drop the trailing period")
    words = CODE_SPAN.sub(SPAN, title).split()
    for i, word in enumerate(words):
        opens = i > 0 and words[i - 1].endswith(":")
        if SPAN not in word and (fix := word_fix(word, i == 0, opens)):
            found.append(fix)
    return found


def exempt(pr: dict) -> bool:
    """Whether the pull request is Dependabot's, whose own titles the contract takes as they are."""
    return ((pr.get("author") or {}).get("login") or "") == DEPENDABOT


def title_hold(pr: dict) -> list[str] | None:
    """The fixes the title owes, or None where nothing is read.

    Nothing is read on a Dependabot pull request, or one no longer open, whose title a retitle no
    longer reaches. Nor is it on a payload that never asked for the title. Every query `status`,
    `wait`, and `merge` read carries it, so that is a payload built by hand.
    """
    if "title" not in pr or exempt(pr) or not is_open(pr):
        return None
    return problems(pr.get("title") or "")


def title_field(pr: dict) -> str | None:
    """The digest's `title=` value, or None to leave the field out where nothing is read."""
    if "title" in pr and exempt(pr) and is_open(pr):
        return EXEMPT
    hold = title_hold(pr)
    return None if hold is None else HELD if hold else OK


def title_lines(pr: dict) -> list[str]:
    """The digest's block naming each fix a held title owes, else nothing."""
    if not (hold := title_hold(pr)):
        return []
    head = (
        "  TITLE NOT CONVENTIONAL: the merge records the title in history, and it breaks the "
        "mechanical part of the title contract, so retitle the pull request as each line below says"
    )
    return [
        head,
        f"    title: {pr.get('title')}",
        *(f"    {fix}" for fix in hold),
    ]


def title_status(pr: dict) -> str:
    """The status line a held title prints, or empty where it holds nothing."""
    if not (hold := title_hold(pr)):
        return ""
    fixes = ". ".join(fix[0].upper() + fix[1:] for fix in hold)
    return (
        f"status={STATUS} the title breaks the mechanical part of the title contract, so retitle "
        f"the pull request, which clears this on the next read with no push. {fixes}"
    )
