#!/usr/bin/env python3
"""Hold each host CLAUDE.md block section to the governance section it says it is kept in sync with.

The host text is a condensed rewording, so the pin is a digest of the governance section rather than a
byte comparison. A moved digest fails here, and re-recording it in the pin file is the human review.

Run as `uv run --no-project --with-requirements tests/requirements.txt python tests/test_claude_md_sync.py`,
or under `-m unittest discover -s tests` the same way.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "spec"))
sys.path.insert(0, str(ROOT / "host-setup" / "agent-safety"))
import canonical_review as cr
import global_rules

KIT = ROOT / "host-setup" / "agent-safety" / "claude"
PINS = KIT / "claude-md-sections.json"
PINS_REL = PINS.relative_to(ROOT).as_posix()

SYNC_CLAIM = re.compile(r"\bkept in sync\b")


def load_pairs() -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = json.loads(PINS.read_text(encoding="utf-8"))["pairs"]
    return pairs


def file_units(*rels: str) -> dict[str, str]:
    """Every unit of these files in this checkout, read through `canonical_review.py`'s own reader."""
    return cr.build_units(dict.fromkeys(rels), cr.disk_reader(ROOT))[0]


def read_unit(key: str) -> str | None:
    """One unit's text by its `<path> > <section>` key, or None where the file or section is gone."""
    return file_units(key.partition(cr.SECTION_DELIM)[0]).get(key)


def drift(pairs: list[dict[str, Any]], read: Callable[[str], str | None]) -> list[str]:
    """One message per pair whose governance section is gone or no longer matches its pinned digest."""
    out: list[str] = []
    for pair in pairs:
        host, section, pinned = pair["host"], pair["section"], pair["digest"]
        text = read(section)
        if text is None:
            out.append(
                f"{section}, which {host} is kept in sync with, no longer exists."
                f" Re-pair the host section in {PINS_REL} with the section that now holds its rules, and review the two."
            )
            continue
        current = cr.digest(text)
        if current != pinned:
            out.append(
                f"{section} no longer matches the digest pinned beside {host} in {PINS_REL}"
                f" (pinned {pinned}, now {current})."
                " Re-recording that digest is the review step."
                " Read the section, confirm the host section still states its rules without contradicting them,"
                f" edit the host section where it does not, then record {current}."
            )
    return out


class LiveTreeCase(unittest.TestCase):
    """The pin file against this checkout's own sections and host blocks."""

    def test_every_pinned_section_matches_its_digest(self) -> None:
        messages = drift(load_pairs(), read_unit)
        self.assertEqual([], messages, "\n".join(messages))

    def test_every_section_claiming_sync_is_pinned(self) -> None:
        blocks = [
            (KIT / name).relative_to(ROOT).as_posix()
            for _, name in global_rules.load_kit().CLAUDE_MD_BLOCKS
        ]
        claiming = {key for key, text in file_units(*blocks).items() if SYNC_CLAIM.search(text)}
        pinned = {pair["host"] for pair in load_pairs()}
        self.assertTrue(
            claiming, "no host block section says it is kept in sync, so this check reads nothing"
        )
        self.assertEqual(
            sorted(claiming - pinned), [], f"host sections claiming sync with no pair in {PINS_REL}"
        )
        self.assertEqual(
            sorted(pinned - claiming),
            [],
            f"pairs in {PINS_REL} whose host section is gone or claims no sync",
        )


class DriftCase(unittest.TestCase):
    """What a failing pin says, against crafted text."""

    PAIR: ClassVar[dict[str, str]] = {
        "host": "host.md > Host",
        "section": "GOVERNANCE.md > Rule",
        "digest": cr.digest("old text\n"),
    }

    def test_a_matching_section_reports_nothing(self) -> None:
        self.assertEqual([], drift([self.PAIR], lambda _key: "old text\n"))

    def test_a_line_ending_change_reports_nothing(self) -> None:
        self.assertEqual([], drift([self.PAIR], lambda _key: "old text\r\n"))

    def test_a_moved_digest_names_the_section_and_the_review_step(self) -> None:
        [message] = drift([self.PAIR], lambda _key: "new text\n")
        self.assertIn("GOVERNANCE.md > Rule", message)
        self.assertIn("host.md > Host", message)
        self.assertIn("Re-recording that digest is the review step", message)
        self.assertIn(cr.digest("new text\n"), message)

    def test_a_missing_section_names_it(self) -> None:
        [message] = drift([self.PAIR], lambda _key: None)
        self.assertIn("GOVERNANCE.md > Rule", message)
        self.assertIn("no longer exists", message)


if __name__ == "__main__":
    unittest.main()
