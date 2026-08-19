#!/usr/bin/env python3
"""Exercise the reusable publish plan's dispatch-ref gate."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "publish-plan-task.yml"


def decision_script() -> str:
    """Extract the shell body Actions executes for the release decision."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    decide = next(i for i, line in enumerate(lines) if line.strip() == "id: decide")
    run = next(i for i in range(decide + 1, len(lines)) if lines[i].strip() == "run: |")
    run_indent = len(lines[run]) - len(lines[run].lstrip())
    start = run + 1
    content_indent = next(
        len(line) - len(line.lstrip()) for line in lines[start:] if line.strip()
    )
    if content_indent <= run_indent:
        raise ValueError("the decide step has no indented run body")
    body = []
    for line in lines[start:]:
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent <= run_indent:
            break
        body.append(line[content_indent:] if line else "")
    return "\n".join(body)


class PublishPlanCase(unittest.TestCase):
    """A manual release accepts only the two long-lived branches."""

    def run_plan(self, ref: str) -> subprocess.CompletedProcess[str]:
        output = Path(self.enterContext(tempfile.TemporaryDirectory())) / "output"
        env = os.environ.copy()
        env.update(
            {
                "ACTOR": "maintainer",
                "EVENT": "workflow_dispatch",
                "GITHUB_OUTPUT": str(output),
                "REF": ref,
            }
        )
        return subprocess.run(
            ["bash", "-c", decision_script()],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

    def test_s8_rejects_a_dispatch_from_an_unsupported_ref(self) -> None:
        result = self.run_plan("feature/unsupported")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::Dispatch a release", result.stdout)

    def test_dispatch_from_develop_publishes(self) -> None:
        result = self.run_plan("develop")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("publish=true", result.stdout)


if __name__ == "__main__":
    unittest.main()
