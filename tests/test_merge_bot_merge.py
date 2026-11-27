#!/usr/bin/env python3
"""Prove the merge-bot's merge step arms on a base with required checks and otherwise waits and merges, per WORKFLOW.md D8.1."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess, run
from typing import Any

import yaml
from host_capability import bash_or_skip

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/merge-bot-task.yml"
HEAD = "a" * 40
OTHER = "b" * 40
PR_URL = "https://github.com/example/widget/pull/7"
OWN_JOB = "Merge bot pull request job / Merge dependabot pull request job"
REQUIRED = [{"type": "pull_request"}, {"type": "required_status_checks"}]
UNREQUIRED = [{"type": "pull_request"}, {"type": "deletion"}]
OPEN = {"state": "open", "merged": False, "head": {"sha": HEAD}}

STUB = """
import json, subprocess, sys
from pathlib import Path

root = Path(sys.argv[1])
args = sys.argv[2:]
with (root / "calls").open("a", encoding="utf-8") as calls:
    calls.write(" ".join(args) + "\\n")
if args[:2] == ["pr", "merge"]:
    sys.exit(0)
path = next(a for a in args[1:] if a.startswith("repos/"))
key = next(k for k in ("rules", "check-runs", "status", "pulls") if "/" + k in path)
served = json.loads((root / "fixtures.json").read_text(encoding="utf-8"))[key]
count = root / ("count-" + key)
index = int(count.read_text()) if count.exists() else 0
count.write_text(str(index + 1))
response = served[min(index, len(served) - 1)]
if "error" in response:
    sys.stderr.write(response["error"] + "\\n")
    sys.exit(1)
expr = args[args.index("--jq") + 1]
sys.exit(subprocess.run(["jq", "-rc", expr], input=json.dumps(response["json"]), text=True).returncode)
"""


def check_run(
    name: str, status: str = "completed", conclusion: str | None = "success"
) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def runs(*checks: dict[str, Any]) -> dict[str, Any]:
    return {"json": {"total_count": len(checks), "check_runs": list(checks)}}


def statuses(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"json": {"statuses": [{"context": c, "state": s} for c, s in pairs]}}


def merge_steps() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Both jobs' merge steps and the parsed workflow, with the aliases resolved as a runner reads them."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    def step(job: str, name: str) -> dict[str, Any]:
        (found,) = [s for s in jobs[job]["steps"] if s.get("name") == name]
        return found

    return (
        step("merge-dependabot", "Merge pull request step"),
        step("merge-app", "Merge App pull request step"),
        workflow,
    )


class MergeStepShapeCase(unittest.TestCase):
    def test_both_jobs_run_one_script_and_env(self) -> None:
        dependabot, app, _ = merge_steps()
        self.assertEqual(dependabot["run"], app["run"])
        self.assertEqual(dependabot["env"], app["env"])

    def test_the_wait_leaves_out_every_job_of_this_workflow(self) -> None:
        dependabot, _, workflow = merge_steps()
        own = json.loads(dependabot["env"]["OWN_JOBS"])
        self.assertEqual(sorted(job["name"] for job in workflow["jobs"].values()), sorted(own))

    def test_the_wait_is_bounded_inside_each_job_timeout(self) -> None:
        dependabot, _, workflow = merge_steps()
        env = dependabot["env"]
        wait = (
            int(env["WAIT_LIMIT_SECONDS"]) + int(env["SETTLE_SECONDS"]) + int(env["POLL_SECONDS"])
        )
        for job in ("merge-dependabot", "merge-app"):
            self.assertLess(wait, int(workflow["jobs"][job]["timeout-minutes"]) * 60, job)


class MergeStepCase(unittest.TestCase):
    """Runs the merge step against a stubbed gh serving a constructed base, pull request, and head checks."""

    def merge(
        self,
        rules: list[dict[str, Any]],
        checks: list[dict[str, Any]],
        status: list[dict[str, Any]] | None = None,
        pulls: list[dict[str, Any]] | None = None,
        **env: str,
    ) -> tuple[CompletedProcess[str], list[str]]:
        dependabot, _, _ = merge_steps()
        fixtures = {
            "rules": [{"json": rules}],
            "check-runs": checks,
            "status": status or [statuses()],
            "pulls": pulls or [{"json": OPEN}],
        }
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (root / "fixtures.json").write_text(json.dumps(fixtures), encoding="utf-8")
            (root / "stub.py").write_text(STUB, encoding="utf-8")
            (bin_dir / "gh").write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{root / "stub.py"}" "{root}" "$@"\n',
                encoding="utf-8",
            )
            (bin_dir / "gh").chmod(0o755)
            result = run(
                [bash_or_skip(), "-c", dependabot["run"]],
                check=False,
                cwd=root,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "REPO": "example/widget",
                    "NUMBER": "7",
                    "BASE_REF": "develop",
                    "HEAD_SHA": HEAD,
                    "PR_URL": PR_URL,
                    "MERGE_METHOD": "--squash",
                    "OWN_JOBS": dependabot["env"]["OWN_JOBS"],
                    "WAIT_LIMIT_SECONDS": "30",
                    "SETTLE_SECONDS": "0",
                    "POLL_SECONDS": "0",
                    **env,
                },
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            calls = root / "calls"
            lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return result, lines

    @staticmethod
    def reads(calls: list[str], endpoint: str) -> int:
        return len([call for call in calls if call.startswith("api") and endpoint in call])

    @staticmethod
    def merges(calls: list[str]) -> list[str]:
        return [call for call in calls if call.startswith("pr merge")]

    def test_a_base_with_required_checks_arms_auto_merge(self) -> None:
        result, calls = self.merge(REQUIRED, [runs()])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            [f"pr merge --auto --squash --match-head-commit {HEAD} {PR_URL}"], self.merges(calls)
        )
        self.assertEqual(0, self.reads(calls, "/check-runs"))

    def test_passing_checks_merge_directly_leaving_out_the_bots_own_job(self) -> None:
        result, calls = self.merge(
            UNREQUIRED,
            [
                runs(
                    check_run("Lint job"),
                    check_run("Docs job", conclusion="skipped"),
                    check_run(OWN_JOB, "in_progress", None),
                )
            ],
            [statuses(("example/ci", "success"))],
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            [f"pr merge --squash --match-head-commit {HEAD} {PR_URL}"], self.merges(calls)
        )
        self.assertIn(
            f"api --paginate repos/example/widget/commits/{HEAD}/check-runs?per_page=100",
            " ".join(calls),
        )

    def test_a_pending_check_is_waited_for(self) -> None:
        result, calls = self.merge(
            UNREQUIRED,
            [runs(check_run("Lint job", "in_progress", None)), runs(check_run("Lint job"))],
            [statuses(("example/ci", "pending")), statuses(("example/ci", "success"))],
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(2, self.reads(calls, "/check-runs"))
        self.assertEqual(1, len(self.merges(calls)))

    def test_a_check_posting_during_the_settle_restarts_it(self) -> None:
        result, calls = self.merge(
            UNREQUIRED,
            [runs(check_run("Lint job")), runs(check_run("Lint job"), check_run("Test job"))],
            SETTLE_SECONDS="1",
            POLL_SECONDS="1",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(3, self.reads(calls, "/check-runs"))
        self.assertEqual(1, len(self.merges(calls)))

    def test_a_failed_check_fails_without_merging(self) -> None:
        result, calls = self.merge(UNREQUIRED, [runs(check_run("Lint job", conclusion="failure"))])
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            "::error::A check on the head failed, so the pull request is not merged: Lint job (failure)",
            result.stdout,
        )
        self.assertEqual([], self.merges(calls))

    def test_a_failed_commit_status_fails_without_merging(self) -> None:
        result, calls = self.merge(
            UNREQUIRED, [runs(check_run("Lint job"))], [statuses(("example/ci", "error"))]
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("example/ci (error)", result.stdout)
        self.assertEqual([], self.merges(calls))

    def test_a_moved_head_fails_without_merging(self) -> None:
        moved = {"json": {**OPEN, "head": {"sha": OTHER}}}
        result, calls = self.merge(
            UNREQUIRED, [runs(check_run("Lint job", "queued", None))], pulls=[{"json": OPEN}, moved]
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"::error::The head moved from {HEAD} to {OTHER}", result.stdout)
        self.assertEqual([], self.merges(calls))

    def test_a_closed_pull_request_ends_without_merging(self) -> None:
        closed = {"json": {**OPEN, "state": "closed"}}
        result, calls = self.merge(UNREQUIRED, [runs()], pulls=[closed])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], self.merges(calls))

    def test_the_bound_fails_without_merging(self) -> None:
        result, calls = self.merge(
            UNREQUIRED, [runs(check_run("Lint job", "in_progress", None))], WAIT_LIMIT_SECONDS="0"
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            "did not all pass and settle within 0 seconds, so the pull request is not merged: Lint job",
            result.stdout,
        )
        self.assertEqual([], self.merges(calls))

    def test_a_refused_check_read_names_the_unaccepted_permission(self) -> None:
        refused = {"error": "gh: Resource not accessible by integration (HTTP 403)"}
        result, calls = self.merge(UNREQUIRED, [refused])
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            "an App installation has not accepted the checks: read permission yet", result.stdout
        )
        self.assertEqual(1, self.reads(calls, "/check-runs"))
        self.assertEqual([], self.merges(calls))


if __name__ == "__main__":
    unittest.main()
