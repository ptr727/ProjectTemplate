#!/usr/bin/env python3
"""Drive the hub's resolve-upstream hook and its host-tool line tracker against constructed data.

The jq filters run on constructed release data. The hook's step runs whole under bash. A stub
curl ahead of the real one on PATH serves constructed files by basename, so no case reaches the
network. The caller and the state file are read for the shape the reusable task diffs against.

Run as `python3 tests/test_resolve_upstream.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from host_capability import bash_or_skip
from workflow_steps import step_script

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / ".github" / "actions" / "resolve-upstream"
ACTION = HOOK / "action.yml"
CALLER = REPO / ".github" / "workflows" / "check-host-tool-lines.yml"
STATE = REPO / "host-tool-lines.json"
STEP = "Resolve host tool lines step"

STUB_CURL = """#!/usr/bin/env bash
set -Eeuo pipefail
out=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out="$2"; shift 2 ;;
    --max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\\n' "$url" >> "$STUB_DIR/requested"
src="$STUB_DIR/${url##*/}"
if [ ! -f "$src" ]; then
  echo "curl: (22) The requested URL returned error: 404" >&2
  exit 22
fi
cp "$src" "$out"
"""


def python_cycle(**lines: str) -> dict[str, dict[str, str]]:
    """Release-cycle data with each `3_14="bugfix"` keyword as line 3.14 at that status."""
    return {key.replace("_", "."): {"status": status} for key, status in lines.items()}


def dotnet_index(*channels: tuple[str, str]) -> dict[str, object]:
    """A release index holding each (channel-version, support-phase) pair."""
    return {
        "releases-index": [
            {"channel-version": channel, "support-phase": phase} for channel, phase in channels
        ]
    }


def jq_or_skip() -> str:
    found = shutil.which("jq")
    if found is None:
        raise unittest.SkipTest("no jq on PATH")
    return found


def run_filter(name: str, data: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [jq_or_skip(), "-r", "-f", str(HOOK / name)],
        input=json.dumps(data),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


class PythonFilterTests(unittest.TestCase):
    def newest(self, data: object) -> str:
        result = run_filter("python.jq", data)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def refused(self, data: object) -> str:
        result = run_filter("python.jq", data)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual("", result.stdout)
        return result.stderr

    def test_newest_released_line_wins_over_unreleased_ones(self) -> None:
        data = python_cycle(
            **{"3_16": "feature", "3_15": "prerelease", "3_14": "bugfix", "3_13": "bugfix"}
        )
        self.assertEqual("3.14", self.newest(data))

    def test_lines_compare_numerically(self) -> None:
        self.assertEqual("3.10", self.newest(python_cycle(**{"3_9": "bugfix", "3_10": "security"})))

    def test_an_end_of_life_line_still_counts_as_released(self) -> None:
        self.assertEqual("2.7", self.newest(python_cycle(**{"2_7": "end-of-life"})))

    def test_an_unknown_status_fails_rather_than_being_guessed(self) -> None:
        stderr = self.refused(python_cycle(**{"3_14": "bugfix", "3_15": "rc"}))
        self.assertIn('line 3.15 has an unknown status "rc"', stderr)

    def test_an_entry_without_a_status_fails(self) -> None:
        self.assertIn("unknown status null", self.refused({"3.14": "bugfix"}))

    def test_a_key_that_is_not_a_line_fails(self) -> None:
        self.assertIn("is not a <major>.<minor> line", self.refused({"main": {"status": "bugfix"}}))

    def test_no_released_line_fails(self) -> None:
        self.assertIn("holds no released line", self.refused(python_cycle(**{"3_16": "feature"})))

    def test_a_document_that_is_not_an_object_fails(self) -> None:
        documents: list[object] = [[], {}, "3.14"]
        for data in documents:
            with self.subTest(data=data):
                self.assertIn("is not a non-empty object", self.refused(data))


class DotnetFilterTests(unittest.TestCase):
    def newest(self, data: object) -> str:
        result = run_filter("dotnet.jq", data)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def refused(self, data: object) -> str:
        result = run_filter("dotnet.jq", data)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual("", result.stdout)
        return result.stderr

    def test_newest_released_major_wins_over_a_preview(self) -> None:
        data = dotnet_index(("12.0", "preview"), ("11.0", "active"), ("10.0", "active"))
        self.assertEqual("11", self.newest(data))

    def test_a_go_live_release_candidate_is_not_released(self) -> None:
        self.assertEqual("10", self.newest(dotnet_index(("11.0", "go-live"), ("10.0", "active"))))

    def test_channels_compare_numerically(self) -> None:
        self.assertEqual("10", self.newest(dotnet_index(("3.1", "eol"), ("10.0", "maintenance"))))

    def test_an_unknown_phase_fails_rather_than_being_guessed(self) -> None:
        stderr = self.refused(dotnet_index(("10.0", "active"), ("11.0", "rc")))
        self.assertIn('channel 11.0 has an unknown support phase "rc"', stderr)

    def test_a_malformed_channel_fails(self) -> None:
        self.assertIn("is not a <major>.<minor> line", self.refused(dotnet_index(("10", "active"))))

    def test_a_non_object_entry_fails(self) -> None:
        self.assertIn("is not an object", self.refused({"releases-index": ["10.0"]}))

    def test_no_released_line_fails(self) -> None:
        self.assertIn("holds no released line", self.refused(dotnet_index(("11.0", "preview"))))

    def test_a_document_without_the_index_fails(self) -> None:
        documents: list[object] = [[], {}, {"releases-index": []}, {"releases-index": "10.0"}]
        for data in documents:
            with self.subTest(data=data):
                self.assertIn("holds no releases-index list", self.refused(data))


class HookStepTests(unittest.TestCase):
    """The hook's step, run whole with its own env, against a stub curl serving constructed data."""

    def setUp(self) -> None:
        jq_or_skip()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.stub = self.root / "stub"
        self.temp = self.root / "runner-temp"
        bin_dir = self.root / "bin"
        for path in (self.stub, self.temp, bin_dir):
            path.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(STUB_CURL, encoding="utf-8", newline="\n")
        curl.chmod(0o755)
        self.output = self.root / "github-output"
        self.output.write_text("", encoding="utf-8")
        step = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"][0]
        self.step_env: dict[str, str] = step["env"]
        self.env = os.environ | self.step_env
        self.env |= {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.stub),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_ACTION_PATH": str(HOOK),
        }

    def serve(self, url_key: str, data: object) -> None:
        name = self.step_env[url_key].rsplit("/", 1)[-1]
        (self.stub / name).write_text(json.dumps(data), encoding="utf-8")

    def run_step(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [bash_or_skip(), "-c", step_script(ACTION, STEP)],
            cwd=self.root,
            env=self.env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=60,
        )

    def test_sources_are_the_upstream_release_data(self) -> None:
        self.assertEqual(
            {
                "PYTHON_URL": "https://peps.python.org/api/release-cycle.json",
                "DOTNET_URL": "https://builds.dotnet.microsoft.com/dotnet/release-metadata/releases-index.json",
            },
            self.step_env,
        )

    def test_writes_one_versions_line_the_task_accepts(self) -> None:
        self.serve("PYTHON_URL", python_cycle(**{"3_15": "prerelease", "3_14": "bugfix"}))
        self.serve("DOTNET_URL", dotnet_index(("11.0", "preview"), ("10.0", "active")))
        result = self.run_step()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            'versions={"dotnet":"10","python":"3.14"}\n',
            self.output.read_text(encoding="utf-8"),
        )
        requested = (self.stub / "requested").read_text(encoding="utf-8").split()
        self.assertEqual([self.step_env["PYTHON_URL"], self.step_env["DOTNET_URL"]], requested)

    def test_a_failed_fetch_fails_the_step_and_writes_nothing(self) -> None:
        self.serve("PYTHON_URL", python_cycle(**{"3_14": "bugfix"}))
        result = self.run_step()
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            f"::error::Could not read the upstream release data at {self.step_env['DOTNET_URL']}",
            result.stdout,
        )
        self.assertEqual("", self.output.read_text(encoding="utf-8"))

    def test_an_unreadable_shape_fails_the_step_and_writes_nothing(self) -> None:
        self.serve("PYTHON_URL", python_cycle(**{"3_14": "rc"}))
        self.serve("DOTNET_URL", dotnet_index(("10.0", "active")))
        result = self.run_step()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("::error::The upstream release data did not hold the shape", result.stdout)
        self.assertIn("unknown status", result.stderr)
        self.assertEqual("", self.output.read_text(encoding="utf-8"))


class TrackerShapeTests(unittest.TestCase):
    """The caller and its state file, read for what the reusable task diffs and opens."""

    def setUp(self) -> None:
        self.job = yaml.safe_load(CALLER.read_text(encoding="utf-8"))["jobs"][
            "check-host-tool-lines"
        ]

    def test_caller_reaches_the_reusable_task(self) -> None:
        self.assertEqual("./.github/workflows/check-upstream-version-task.yml", self.job["uses"])

    def test_the_bump_waits_for_a_human_on_develop(self) -> None:
        self.assertIs(False, self.job["with"]["auto-merge"])
        self.assertEqual(["develop"], json.loads(self.job["with"]["branches"]))

    def test_the_state_file_is_not_the_upstream_wrapper_one(self) -> None:
        """The task's default name is an upstream-wrapper detect hint in spec/project-types.json."""
        self.assertNotEqual("upstream-version.json", self.job["with"]["state-file"])
        self.assertEqual(STATE.name, self.job["with"]["state-file"])

    def test_state_file_holds_the_keys_the_hook_writes_in_the_task_canonical_form(self) -> None:
        """The task rewrites the file as `jq -S .` prints it, so any other form is a diff of its own."""
        text = STATE.read_text(encoding="utf-8")
        state = json.loads(text)
        self.assertEqual({"dotnet", "python"}, set(state))
        self.assertTrue(all(isinstance(value, str) for value in state.values()))
        self.assertEqual(json.dumps(state, indent=2, sort_keys=True) + "\n", text)


if __name__ == "__main__":
    unittest.main()
