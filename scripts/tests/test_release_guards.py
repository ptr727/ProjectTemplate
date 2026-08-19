"""Protect release and audit boundaries from fail-open regressions."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class ReleaseGuardCase(unittest.TestCase):
    """Publishing and audit discovery require their prerequisite checks to succeed."""

    def test_publish_requires_successful_validation(self) -> None:
        workflow = (REPO / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
        files_spec = (REPO / "spec/files.json").read_text(encoding="utf-8")

        self.assertIn(
            "if: ${{ needs.plan.outputs.publish == 'true' && needs.validate.result == 'success' }}",
            workflow,
        )
        self.assertIn("\"needs.validate.result == 'success'\"", files_spec)

    def test_audit_root_probe_fails_before_local_path_checks(self) -> None:
        audit = (REPO / "AUDIT.md").read_text(encoding="utf-8")
        root_probe = (
            'root_paths=$(gh api "repos/<owner>/<repo>/contents?ref=<ground>" '
            "--jq '.[].path') || exit 1"
        )

        self.assertIn(root_probe, audit)
        self.assertIn('has() { grep -Fxq "$1" <<<"$root_paths"; }', audit)
        self.assertNotIn(
            'gh api "repos/<owner>/<repo>/contents/$1?ref=<ground>" >/dev/null 2>&1',
            audit,
        )


if __name__ == "__main__":
    unittest.main()
