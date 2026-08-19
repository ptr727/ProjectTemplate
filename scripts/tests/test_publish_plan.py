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
    start = lines.index("        run: |") + 1
    body = []
    for line in lines[start:]:
        if line and not line.startswith("          "):
            break
        body.append(line[10:] if line else "")
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
