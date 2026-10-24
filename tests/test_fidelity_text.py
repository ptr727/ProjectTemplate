#!/usr/bin/env python3
"""Exercise spec/fidelity_text.py's heading, code-block, and region reading on constructed documents."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spec"))
import fidelity_text


class HeadingCase(unittest.TestCase):
    def test_a_heading_inside_a_code_block_is_content(self) -> None:
        text = "## One\n\n```md\n## Fenced\n```\n\n    ## Indented\n\n   ## Two ##\n"
        self.assertEqual(
            [(h.line, h.level, h.text) for h in fidelity_text.headings(text)],
            [(0, 2, "One"), (8, 2, "Two")],
        )

    def test_a_quoted_or_listed_heading_opens_no_region(self) -> None:
        text = "## Keep\n\n> ## Quoted\n\n- item\n\n  ## Listed\n\n## Drop\n\nreal\n"
        self.assertEqual([h.text for h in fidelity_text.headings(text)], ["Keep", "Drop"])
        self.assertEqual(fidelity_text.section_span(text, "Drop"), (8, None))

    def test_a_setext_heading_opens_no_region(self) -> None:
        self.assertEqual(fidelity_text.headings("Title\n-----\n\n## One\n"), [(3, 2, "One")])


class CodeBlockCase(unittest.TestCase):
    def test_a_closed_fence_reads_closed_at_any_depth(self) -> None:
        for text in (
            "```\na\n```\n",
            "```\na\n```",
            "> ```\n> a\n> ```\n",
            "- item\n\n  ~~~\n  a\n  ~~~\n",
        ):
            with self.subTest(text=text):
                self.assertEqual([b.closed for b in fidelity_text.code_blocks(text)], [True])

    def test_a_fence_with_no_closing_line_reads_open(self) -> None:
        for text in (
            "```\na\n",
            "```\na",
            "```\n",
            "```",
            "```\na\n\n\n",
            "> ```\n> a\n\nafter\n",
            "> ```\n> a",
        ):
            with self.subTest(text=text):
                self.assertEqual([b.closed for b in fidelity_text.code_blocks(text)], [False])

    def test_code_lines_cover_fenced_and_indented_blocks(self) -> None:
        text = "text\n\n```\na\n```\n\n    code\n\nmore\n"
        self.assertEqual(fidelity_text.code_lines(text), frozenset({2, 3, 4, 6}))


class SectionCase(unittest.TestCase):
    def test_the_last_region_runs_to_the_end(self) -> None:
        text = "intro\n\n## Alpha\n\na\n\n## Beta\n\nb\n"
        self.assertEqual(fidelity_text.section_span(text, "alpha"), (2, 6))
        self.assertEqual(fidelity_text.section_span(text, "Beta"), (6, None))
        self.assertIsNone(fidelity_text.section_span(text, "Gamma"))

    def test_strip_sections_keeps_an_identical_passage_elsewhere(self) -> None:
        text = "## Keep\n\nsame\n\n## Drop\n\nsame\n"
        self.assertEqual(fidelity_text.strip_sections(text, ["drop"]), "## Keep\n\nsame\n")


if __name__ == "__main__":
    unittest.main()
