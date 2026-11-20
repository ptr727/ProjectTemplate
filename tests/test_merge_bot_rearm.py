#!/usr/bin/env python3
"""Prove the merge-bot's re-arm job decides who pushed the head from the branch activity, per WORKFLOW.md D8.1."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess, run

from host_capability import bash_or_skip
from workflow_steps import step_script

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/merge-bot-task.yml"
APP = "example-app[bot]"
AUTHOR = "dependabot[bot]"
HEAD = "a" * 40
OTHER = "b" * 40


class RearmPusherCase(unittest.TestCase):
    """Runs the re-arm step against a stubbed gh that serves a constructed pull request and activity log."""

    def rearm(self, activity: list[dict[str, object]]) -> tuple[CompletedProcess[str], list[str]]:
        script = step_script(WORKFLOW, "Re-arm auto-merge step")
        pr = {
            "state": "OPEN",
            "headRefOid": HEAD,
            "baseRef": {"target": {"oid": "c" * 40}},
            "autoMergeRequest": None,
            "headRef": {"target": {"committedDate": "2026-01-01T00:00:00Z"}},
            "timelineItems": {
                "nodes": [
                    {
                        "createdAt": "2026-01-02T00:00:00Z",
                        "reason": "a refused merge",
                        "reasonCode": "example",
                        "disabler": {"login": APP},
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (root / "pr.json").write_text(json.dumps(pr), encoding="utf-8")
            (root / "activity.json").write_text(json.dumps(activity), encoding="utf-8")
            calls = root / "calls"
            (bin_dir / "gh").write_text(
                "#!/bin/sh\n"
                f'echo "$*" >> "{calls}"\n'
                'case "$1 $2" in\n'
                f'  "api graphql") exec cat "{root / "pr.json"}" ;;\n'
                f'  "api -X") exec cat "{root / "activity.json"}" ;;\n'
                '  "pr merge") exit 0 ;;\n'
                "esac\n"
                "exit 9\n",
                encoding="utf-8",
            )
            (bin_dir / "gh").chmod(0o755)
            result = run(
                [bash_or_skip(), "-c", script],
                check=False,
                cwd=root,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "REPO": "example/widget",
                    "NUMBER": "7",
                    "PR_URL": "https://github.com/example/widget/pull/7",
                    "PR_AUTHOR": AUTHOR,
                    "HEAD_REF": "dependabot/example/widget-1.2.3",
                    "APP_BOT": APP,
                    "MERGE_METHOD": "--squash",
                    "SETTLE_SECONDS": "0",
                    "SETTLE_LIMIT_SECONDS": "5",
                    "POLL_SECONDS": "0",
                },
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return result, lines

    def test_the_bot_pushing_the_head_re_arms_pinned_to_it(self) -> None:
        result, calls = self.rearm(
            [
                {"after": OTHER, "activity_type": "push", "actor": {"login": "example-maintainer"}},
                {"after": HEAD, "activity_type": "force_push", "actor": {"login": AUTHOR}},
            ]
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            "api -X GET repos/example/widget/activity -f ref=refs/heads/dependabot/example/widget-1.2.3 -F per_page=30",
            calls,
        )
        self.assertIn(
            f"pr merge --auto --squash --match-head-commit {HEAD} https://github.com/example/widget/pull/7",
            calls,
        )

    def test_a_person_pushing_a_bot_authored_head_leaves_auto_merge_off(self) -> None:
        result, calls = self.rearm(
            [{"after": HEAD, "activity_type": "push", "actor": {"login": "example-maintainer"}}]
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            "::notice::example-maintainer pushed the head rather than the bot", result.stdout
        )
        self.assertFalse([call for call in calls if call.startswith("pr merge")])

    def test_a_head_with_no_recorded_push_fails(self) -> None:
        result, calls = self.rearm(
            [{"after": OTHER, "activity_type": "push", "actor": {"login": AUTHOR}}]
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("::error::No push of the head is recorded", result.stdout)
        self.assertFalse([call for call in calls if call.startswith("pr merge")])


if __name__ == "__main__":
    unittest.main()
