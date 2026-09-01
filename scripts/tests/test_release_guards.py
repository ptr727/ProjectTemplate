#!/usr/bin/env python3
"""Protect release and audit boundaries from fail-open regressions."""

from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import run

REPO = Path(__file__).resolve().parents[2]


class ReleaseGuardCase(unittest.TestCase):
    """Publishing and audit discovery require their prerequisite checks to succeed."""

    def test_pypi_artifact_name_matches_contracts_and_consumers(self) -> None:
        canonical_name = "pypi-build-"
        legacy_name = "pypilibrary" + "-build-"
        required_paths = (
            "GOVERNANCE.md",
            "WORKFLOW.md",
            ".github/actions/pypi-build-default/action.yml",
            "docs/reusable-workflows.md",
        )

        for relative_path in required_paths:
            with self.subTest(path=relative_path):
                content = (REPO / relative_path).read_text(encoding="utf-8")
                self.assertIn(canonical_name, content)

        tracked_text = run(
            ["git", "grep", "-n", legacy_name],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual("", tracked_text.stdout)
        self.assertEqual(1, tracked_text.returncode)

    def test_nuget_artifact_name_matches_contracts_and_consumers(self) -> None:
        canonical_name = "nuget-build-"
        legacy_name = "nuget-push" + "-default"
        required_paths = (
            "GOVERNANCE.md",
            "WORKFLOW.md",
            ".github/actions/nuget-build-default/action.yml",
            "docs/reusable-workflows.md",
        )

        for relative_path in required_paths:
            with self.subTest(path=relative_path):
                content = (REPO / relative_path).read_text(encoding="utf-8")
                self.assertIn(canonical_name, content)

        # A typo on either side leaves the publish job downloading nothing.
        # Anchoring to end of line rejects a name that merely starts with the expected one.
        producer = (REPO / ".github/actions/nuget-build-default/action.yml").read_text(
            encoding="utf-8"
        )
        consumer = (REPO / "docs/reusable-workflows.md").read_text(encoding="utf-8")
        self.assertRegex(
            producer, r"(?m)^[ \t]*name: nuget-build-\$\{\{ inputs\.branch \}\}[ \t]*$"
        )
        self.assertRegex(
            consumer, r"(?m)^[ \t]*name: nuget-build-\$\{\{ github\.ref_name \}\}[ \t]*$"
        )
        self.assertIn('nuget-build-${{ github.ref_name }}\\"', consumer)

        tracked_text = run(
            ["git", "grep", "-n", legacy_name],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual("", tracked_text.stdout)
        self.assertEqual(1, tracked_text.returncode)

    def test_hub_release_task_never_pushes_to_a_package_registry(self) -> None:
        # A push from here carries this repository's job_workflow_ref claim, which every adopter's registry rejects.
        forbidden = ("NuGet/login", "nuget push", "gh-action-pypi-publish")
        hub_owned = (
            ".github/workflows/build-release-task.yml",
            ".github/actions/nuget-build-default/action.yml",
            ".github/actions/pypi-build-default/action.yml",
        )

        for relative_path in hub_owned:
            content = (REPO / relative_path).read_text(encoding="utf-8")
            for marker in forbidden:
                with self.subTest(path=relative_path, marker=marker):
                    self.assertNotIn(marker, content)

        # The caller stub is where those pushes belong, so the documented stub must still carry them.
        # Without this floor the assertions above would also pass if the release chain stopped publishing entirely.
        stub_text = (REPO / "docs/reusable-workflows.md").read_text(encoding="utf-8")
        for marker in ("NuGet/login", "nuget push", "gh-action-pypi-publish"):
            with self.subTest(marker=marker):
                self.assertIn(marker, stub_text)

    def test_publish_requires_successful_validation(self) -> None:
        workflow = (REPO / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
        files_spec = (REPO / "spec/files.json").read_text(encoding="utf-8")

        self.assertIn(
            "if: ${{ needs.plan.outputs.publish == 'true' && needs.validate.result == 'success' }}",
            workflow,
        )
        self.assertIn("\"needs.validate.result == 'success'\"", files_spec)

    def test_audit_probes_fail_before_local_path_checks(self) -> None:
        audit = (REPO / "AUDIT.md").read_text(encoding="utf-8")
        lines = audit.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("  dependabot_content="))
        probe = "\n".join(line.removeprefix("  ") for line in lines[start : start + 6])
        fake_api = r"""
gh() {
  case "$2" in
    repos/*/contents/.github/dependabot.yml\?*) printf '%s\n' '- package-ecosystem: github-actions' '- package-ecosystem: devcontainers' | base64 ;;
    repos/*/contents/.github\?*) printf '%s\n' .github/dependabot.yml .github/workflows ;;
    repos/*/contents\?*) printf '%s\n' .devcontainer .github ;;
    *) return 17 ;;
  esac
}
"""

        success = run(
            ["bash", "-c", f"{fake_api}\n{probe}\nhas .github/workflows && has .devcontainer"],
            check=False,
        )
        failure = run(
            ["bash", "-c", f"gh() {{ return 17; }}\n{probe}\nexit 0"],
            check=False,
        )
        decode_failure = run(
            ["bash", "-c", f"gh() {{ printf invalid; }}\n{probe}\nexit 0"],
            check=False,
        )

        self.assertEqual(0, success.returncode)
        self.assertNotEqual(0, failure.returncode)
        self.assertNotEqual(0, decode_failure.returncode)
        self.assertNotIn(
            'gh api "repos/<owner>/<repo>/contents/$1?ref=<ground>" >/dev/null 2>&1',
            audit,
        )

    def test_audit_bash_blocks_are_not_labeled_as_posix_shell(self) -> None:
        audit_lines = (REPO / "AUDIT.md").read_text(encoding="utf-8").splitlines()
        bash_only = ("<(", "<<<", "$'", "[[")
        mislabeled = []
        fence_label = ""
        fence_start = 0
        fence_lines: list[str] = []

        for number, line in enumerate(audit_lines, start=1):
            stripped = line.strip()
            if not fence_label and stripped.startswith("```"):
                fence_label = stripped.removeprefix("```").split(maxsplit=1)[0]
                fence_start = number
            elif fence_label and stripped == "```":
                if fence_label in {"sh", "shell"} and any(
                    token in "\n".join(fence_lines) for token in bash_only
                ):
                    mislabeled.append((fence_start, fence_label))
                fence_label = ""
                fence_lines = []
            elif fence_label:
                fence_lines.append(line)

        self.assertEqual([], mislabeled)


if __name__ == "__main__":
    unittest.main()
