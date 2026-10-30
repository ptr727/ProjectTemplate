#!/usr/bin/env python3
"""Exercise spec/fidelity_text.py's heading, code-block, region, and link reading on constructed documents."""

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


class LinkCase(unittest.TestCase):
    def test_the_first_of_definitions_folding_together_wins(self) -> None:
        text = (
            "[docs]: https://example.com/a\n"
            "[docs]: https://example.com/b\n"
            "[Docs]: https://example.com/c\n"
        )
        self.assertEqual(
            fidelity_text.link_definitions(text),
            [fidelity_text.LinkDefinition("docs", "https://example.com/a", 0)],
        )
        self.assertEqual(fidelity_text.reference_key("Docs"), fidelity_text.reference_key("docs"))

    def test_a_definition_is_read_only_where_the_parser_reads_one(self) -> None:
        text = (
            "Para text\n[para]: p.md\n\n```md\n[fenced]: f.md\n```\n\n"
            "[spaced]: <a b.md>\n[split]:\n  s.md\n\n> [quoted]: q.md\n"
        )
        self.assertEqual(
            [(d.label, d.url, d.line) for d in fidelity_text.link_definitions(text)],
            [("spaced", "a b.md", 7), ("split", "s.md", 8), ("quoted", "q.md", 11)],
        )

    def test_an_empty_or_wrapped_definition_is_read_as_written(self) -> None:
        text = "[empty]: <>\n\n[two\nlines]: t.md\n"
        self.assertEqual(
            [(d.label, d.url) for d in fidelity_text.link_definitions(text)],
            [("empty", ""), ("two\nlines", "t.md")],
        )

    def test_a_link_and_its_definition_read_as_one_target(self) -> None:
        text = "Read [n][n].\n\n[n]: ./docs/a%20b.md\n"
        self.assertEqual(fidelity_text.link_targets(text), ["./docs/a b.md"])

    def test_link_targets_read_every_link_form_and_skip_code(self) -> None:
        text = (
            "See [r][ref], [u](../\u00fc.md), <https://auto.example>, `[c](code.md)`, ![i](i.png).\n\n"
            "    [indented](no.md)\n\n[ref]: ../x.md\n[unused]: other.md\n"
        )
        self.assertEqual(
            fidelity_text.link_targets(text),
            ["../x.md", "../\u00fc.md", "https://auto.example", "i.png", "other.md"],
        )


if __name__ == "__main__":
    unittest.main()
