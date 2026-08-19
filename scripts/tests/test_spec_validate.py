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


if __name__ == "__main__":
    unittest.main()
