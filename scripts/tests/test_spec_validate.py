#!/usr/bin/env python3
"""Exercise carried-link portability checks against a crafted manifest."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "spec"))
import validate


class CarriedRelativeLinkCase(unittest.TestCase):
    """A hub-valid relative link must also resolve after its section is carried."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.baseline = [
            {
                "path": "GOVERNANCE.md",
                "appliesTo": "*",
                "sections": [{"name": "Rule", "fidelity": "verbatim"}],
            },
            {"path": "WORKFLOW.md", "appliesTo": "*"},
        ]

    def write_governance(self, target: str) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            f"# Governance\n\n## Rule\n\nRead [the contract]({target}).\n",
            encoding="utf-8",
        )

    def test_rejects_a_hub_only_relative_target(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "hub-only.md").write_text("# Hub only\n", encoding="utf-8")
        self.write_governance("./docs/hub-only.md")

        self.assertEqual(
            validate.carried_link_errors(self.root, self.baseline),
            [
                (
                    "files.json: GOVERNANCE.md section 'Rule' links to relative target "
                    "'./docs/hub-only.md', which is not universally carried"
                )
            ],
        )

    def test_accepts_a_universally_carried_relative_target(self) -> None:
        self.write_governance("./WORKFLOW.md#contract")

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_rejects_an_absolute_template_repository_target(self) -> None:
        self.write_governance(
            "https://github.com/ptr727/ProjectTemplate/blob/main/.github/workflows/publish-release.yml"
        )

        self.assertEqual(
            validate.carried_link_errors(self.root, self.baseline),
            [
                (
                    "files.json: GOVERNANCE.md section 'Rule' links to the template repository "
                    "at 'https://github.com/ptr727/ProjectTemplate/blob/main/.github/workflows/"
                    "publish-release.yml'"
                )
            ],
        )

    def test_rejects_the_template_repository_root_with_a_fragment(self) -> None:
        self.write_governance("https://github.com/ptr727/ProjectTemplate#readme")

        self.assertEqual(1, len(validate.carried_link_errors(self.root, self.baseline)))

    def test_ignores_markdown_syntax_inside_inline_code(self) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            "# Governance\n\n## Rule\n\nStrip the `[text](url)` syntax.\n",
            encoding="utf-8",
        )

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_ignores_relative_links_inside_fenced_code(self) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            "# Governance\n\n## Rule\n\n~~~markdown\n[hub only](./docs/hub-only.md)\n~~~\n",
            encoding="utf-8",
        )

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_accepts_a_universally_carried_reference_target(self) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            "# Governance\n\n## Rule\n\nRead [the contract][contract].\n\n"
            "[contract]: ./WORKFLOW.md#contract\n",
            encoding="utf-8",
        )

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_accepts_a_different_repository_with_the_template_name_as_a_prefix(self) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            "# Governance\n\n## Rule\n\n"
            "Read [the related repository](https://github.com/ptr727/ProjectTemplate-fork).\n",
            encoding="utf-8",
        )

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_rejects_a_hub_only_target_in_a_whole_intent_file(self) -> None:
        (self.root / "AUDIT.md").write_text(
            "# Audit\n\nRead [the registry](./registry/repos.json).\n",
            encoding="utf-8",
        )
        baseline = [{"path": "AUDIT.md", "fidelity": "intent", "appliesTo": "*"}]

        self.assertEqual(
            validate.carried_link_errors(self.root, baseline),
            [
                (
                    "files.json: AUDIT.md whole file links to relative target "
                    "'./registry/repos.json', which is not universally carried"
                )
            ],
        )

    def test_checks_an_explicit_whole_intent_file_that_also_lists_sections(self) -> None:
        (self.root / "AUDIT.md").write_text(
            "# Audit\n\nRead [the registry](./registry/repos.json).\n",
            encoding="utf-8",
        )
        baseline = [
            {
                "path": "AUDIT.md",
                "fidelity": "intent",
                "whole": True,
                "appliesTo": "*",
                "sections": [{"name": "Audit", "fidelity": "intent"}],
            }
        ]

        self.assertEqual(1, len(validate.carried_link_errors(self.root, baseline)))


class DescriptionErrorsCase(unittest.TestCase):
    """registry/repos.json's optional `description` (GOVERNANCE.md "Repository Details")."""

    def test_a_plain_short_sentence_is_clean(self) -> None:
        self.assertEqual(validate.description_errors("Fixture", "A short tagline."), [])

    def test_whitespace_only_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "   "),
            ["Fixture: description must be a non-empty string"],
        )

    def test_an_explicit_null_is_rejected_rather_than_read_as_absent(self) -> None:
        # The per-repo loop tests "description" in repo rather than is not None.
        # An explicit "description": null therefore reaches here as `None` instead of being skipped as absent.
        self.assertEqual(
            validate.description_errors("Fixture", None),
            ["Fixture: description must be a non-empty string"],
        )

    def test_a_non_string_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", 42),
            ["Fixture: description must be a non-empty string"],
        )

    def test_an_inline_markdown_link_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "See [docs](https://example.test) for more."),
            ["Fixture: description carries Markdown links - keep it link-free plain text"],
        )

    def test_a_reference_style_markdown_link_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "See [docs][ref] for more."),
            ["Fixture: description carries Markdown links - keep it link-free plain text"],
        )

    def test_leading_or_trailing_whitespace_is_rejected(self) -> None:
        # Not silently trimmed here, even though spec/audit.py and configure.sh both strip it defensively.
        # Rejecting it at the source keeps the registry's own text the exact canonical form every mirror carries.
        self.assertEqual(
            validate.description_errors("Fixture", "  A short tagline.  "),
            [
                "Fixture: description must be plain single-line text with no leading or trailing whitespace"
            ],
        )

    def test_an_embedded_newline_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "A tagline.\nA second line."),
            [
                "Fixture: description must be plain single-line text with no leading or trailing whitespace"
            ],
        )

    def test_exactly_the_cap_is_clean(self) -> None:
        self.assertEqual(validate.description_errors("Fixture", "a" * 100), [])

    def test_over_the_cap_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "a" * 101),
            ["Fixture: description is 101 characters, over the 100-char limit"],
        )


if __name__ == "__main__":
    unittest.main()
