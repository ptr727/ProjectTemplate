#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's fleet-project payload, lookups, and link write by running its own lines.

The shell is lifted out of the file rather than restated here, so an edit that removes the
behavior fails these tests instead of leaving a reimplementation to agree with itself. Each
harness lifts the whole region under test, and the two cases that stub a helper stub only a
helper this file tests separately, so nothing under test is replaced by a stand-in.

Every case runs offline: `gh` is a shell function the harness defines, which is what lets the
link write be exercised at all without firing a real mutation at GitHub.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIGURE = ROOT / "repo-config" / "configure.sh"
PAYLOAD = ROOT / "repo-config" / "project.json"

PROJECT_ID = "PVT_kwHOfixture"
REPO_ID = "R_kgDOfixture"


def lift(pattern: str) -> str:
    """The one region of configure.sh matching `pattern`, or a failure naming what was missing."""
    text = CONFIGURE.read_text(encoding="utf-8")
    found = re.findall(pattern, text, re.MULTILINE | re.DOTALL)
    if len(found) != 1:
        raise AssertionError(
            f"expected one match in {CONFIGURE.name} for {pattern!r}, found {len(found)}"
        )
    return found[0]


def require(*tools: str) -> str:
    """The bash path, skipping instead of failing where a tool the harness shells out to is absent."""
    for tool in tools:
        if shutil.which(tool) is None:
            raise unittest.SkipTest(f"no {tool} on PATH, so the script's own lines cannot be run")
    return str(shutil.which("bash"))


def run_bash(script: str, *tools: str) -> subprocess.CompletedProcess[str]:
    """The script under the same shell options configure.sh sets, with a bounded wait.

    The options are lifted rather than typed, because a harness running without them is blind to
    exactly the error handling the lines under test rely on.
    """
    bash = require("bash", *tools)
    options = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
    return subprocess.run(
        [bash, "-c", f"{options}\n{script}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


JQR = lift(r"^(jqr\(\) \{ jq -r .*?\}\n)")
PAYLOAD_OK = lift(r"^(project_payload_ok\(\) \{\n.*?\n\}\n)")
NODE_ID = lift(r"^(project_node_id\(\) \{ # owner number title\n.*?\n\}\n)")
REPO_PROJECTS = lift(r"^(repo_projects\(\) \{\n.*?\n\}\n)")
APPLY_PROJECT = lift(r"^(apply_project\(\) \{\n.*?\n\}\n)")
CHECK_PROJECT = r"^(check_project\(\) \{\n.*?\n\}\n)"
REPORTERS = r"(FAILED=0\nnote\(\).*?\nfail\(\) \{.*?\n\})"
JQ_HAS = r"^(jq_has\(\) \{ jq -e .*?\}\n)"
ASSERT = r"^(assert\(\) \{\n.*?\n\}\n)"


def gh_stub(stdout: str, status: int = 0, log: Path | None = None) -> str:
    """A `gh` that answers with one canned document, optionally recording each call it was given."""
    record = f'  printf "%s\\n" "$*" >>{shlex.quote(str(log))};\n' if log is not None else ""
    return f"gh() {{\n{record}  printf '%s' {json.dumps(stdout)}\n  return {status}\n}}\n"


class PayloadContractCase(unittest.TestCase):
    """The contract apply pre-flights before any write, so a bad payload never half-applies a repo."""

    def payload_ok(self, document: str) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            path.write_text(document, encoding="utf-8")
            result = run_bash(
                f"project_file={shlex.quote(str(path))}\n{PAYLOAD_OK}"
                "if project_payload_ok; then echo yes; else echo no; fi\n",
                "jq",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip() == "yes"

    def test_the_committed_payload_meets_its_own_contract(self) -> None:
        """The file every fleet apply reads is the one case that must never fail."""
        self.assertTrue(self.payload_ok(PAYLOAD.read_text(encoding="utf-8")))

    def test_a_field_outside_the_contract_is_refused(self) -> None:
        """Each case names the one field it breaks, so a loosened test fails for a readable reason."""
        for label, document in (
            ("no owner", '{"number": 1, "title": "Fleet Engineering"}'),
            ("empty owner", '{"owner": "", "number": 1, "title": "Fleet Engineering"}'),
            ("no title", '{"owner": "ptr727", "number": 1}'),
            ("empty title", '{"owner": "ptr727", "number": 1, "title": ""}'),
            ("no number", '{"owner": "ptr727", "title": "Fleet Engineering"}'),
            ("number as a string", '{"owner": "ptr727", "number": "1", "title": "Fleet"}'),
            ("fractional number", '{"owner": "ptr727", "number": 1.5, "title": "Fleet"}'),
            (
                "integer written as a decimal",
                '{"owner": "ptr727", "number": 1.0, "title": "Fleet"}',
            ),
            (
                "integer written in exponent form",
                '{"owner": "ptr727", "number": 1e2, "title": "Fleet"}',
            ),
            (
                "number past a 32-bit Int",
                '{"owner": "ptr727", "number": 2147483648, "title": "Fleet"}',
            ),
            ("zero number", '{"owner": "ptr727", "number": 0, "title": "Fleet"}'),
            ("negative number", '{"owner": "ptr727", "number": -1, "title": "Fleet"}'),
            ("tab in title", '{"owner": "ptr727", "number": 1, "title": "Fleet\\tEngineering"}'),
            ("line break in owner", '{"owner": "ptr\\n727", "number": 1, "title": "Fleet"}'),
            ("owner that is not a string", '{"owner": 727, "number": 1, "title": "Fleet"}'),
            ("title that is not a string", '{"owner": "ptr727", "number": 1, "title": true}'),
            ("not an object", '["ptr727", 1]'),
            ("not JSON", "{"),
        ):
            with self.subTest(label=label):
                self.assertFalse(self.payload_ok(document))


class ProjectLookupCase(unittest.TestCase):
    """Resolving the declared number to a node id, and refusing every answer that is not the declared project."""

    def lookup(
        self, response: object, status: int = 0, title: str = "Fleet Engineering"
    ) -> subprocess.CompletedProcess[str]:
        document = response if isinstance(response, str) else json.dumps(response)
        script = (
            f"project_file=/fixture/project.json\n{JQR}{gh_stub(document, status)}{NODE_ID}"
            f"project_node_id ptr727 1 {title!r}\n"
        )
        return run_bash(script, "jq", "sed")

    def test_a_matching_project_resolves_to_its_node_id(self) -> None:
        result = self.lookup(
            {
                "data": {
                    "repositoryOwner": {
                        "projectV2": {"id": PROJECT_ID, "title": "Fleet Engineering"}
                    }
                }
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), PROJECT_ID)

    def test_a_number_owning_no_project_is_refused(self) -> None:
        """A null owner and a null project are the same answer here, and neither may resolve to an id."""
        for label, response in (
            ("no project", {"data": {"repositoryOwner": {"projectV2": None}}}),
            ("no owner", {"data": {"repositoryOwner": None}}),
        ):
            with self.subTest(label=label):
                result = self.lookup(response)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("No project number 1 owned by ptr727", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_a_reused_number_titled_otherwise_is_refused(self) -> None:
        """The guard the title exists for: a recreated project can hold the number the payload declares."""
        result = self.lookup(
            {
                "data": {
                    "repositoryOwner": {"projectV2": {"id": PROJECT_ID, "title": "Someone Else"}}
                }
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is titled 'Someone Else'", result.stderr)
        self.assertIn("'Fleet Engineering'", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_failed_call_is_refused_rather_than_read_as_no_project(self) -> None:
        result = self.lookup("", status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to read project number 1 owned by ptr727", result.stderr)
        self.assertEqual(result.stdout, "")


class RepoProjectsCase(unittest.TestCase):
    """Reading the repository's own id and its link list, where a missing repository must not read as no links."""

    def projects(self, response: object, status: int = 0) -> subprocess.CompletedProcess[str]:
        document = response if isinstance(response, str) else json.dumps(response)
        script = f"repo=ptr727/Fixture\n{gh_stub(document, status)}{REPO_PROJECTS}repo_projects\n"
        return run_bash(script, "jq")

    def test_the_id_and_every_linked_project_are_emitted(self) -> None:
        result = self.projects(
            {
                "data": {
                    "repository": {
                        "id": REPO_ID,
                        "projectsV2": {"nodes": [{"id": PROJECT_ID}, {"id": "PVT_other"}]},
                    }
                }
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"id": REPO_ID, "projects": [PROJECT_ID, "PVT_other"]}
        )

    def test_a_repository_that_is_not_there_fails_rather_than_reporting_no_links(self) -> None:
        """A response carrying data and a null repository is the fail-open case this guard exists for."""
        result = self.projects({"data": {"repository": None}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No repository ptr727/Fixture", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_full_page_fails_rather_than_reading_as_the_whole_list(self) -> None:
        """At the cap the list may be truncated, which would read as the fleet link missing."""
        nodes = [{"id": f"PVT_{i}"} for i in range(100)]
        result = self.projects(
            {"data": {"repository": {"id": REPO_ID, "projectsV2": {"nodes": nodes}}}}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("100 linked projects returned", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_page_below_the_cap_is_emitted(self) -> None:
        """The guard is the count test itself, so the ordinary case must still pass through it."""
        nodes = [{"id": f"PVT_{i}"} for i in range(99)]
        result = self.projects(
            {"data": {"repository": {"id": REPO_ID, "projectsV2": {"nodes": nodes}}}}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["projects"]), 99)

    def test_a_failed_call_fails(self) -> None:
        result = self.projects("", status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to read the projects linked to ptr727/Fixture", result.stderr)


class ApplyLinkCase(unittest.TestCase):
    """The link write itself: never repeated, and never reported without the response confirming it."""

    def apply(
        self, linked: list[str], mutation_repo_id: str = REPO_ID
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        """apply_project against a stubbed link list, recording every `gh` call it makes.

        `project_node_id` and `repo_projects` are stubbed because each is exercised whole by its
        own case above, which leaves this case measuring the one thing it is about: whether the
        mutation runs, and what the function does with what comes back.
        """
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "project.json"
            payload.write_text(
                json.dumps({"owner": "ptr727", "number": 1, "title": "Fleet Engineering"}),
                encoding="utf-8",
            )
            log = Path(tmp) / "gh-calls.txt"
            log.touch()
            response = json.dumps(
                {"data": {"linkProjectV2ToRepository": {"repository": {"id": mutation_repo_id}}}}
            )
            live = json.dumps({"id": REPO_ID, "projects": linked})
            script = (
                f"repo=ptr727/Fixture\nproject_file={shlex.quote(str(payload))}\n{JQR}"
                f"{gh_stub(response, log=log)}"
                f'project_node_id() {{ printf "%s\\n" {PROJECT_ID}; }}\n'
                f"repo_projects() {{ printf '%s' {json.dumps(live)}; }}\n"
                f"{APPLY_PROJECT}apply_project\n"
            )
            result = run_bash(script, "jq", "sed")
            return result, log.read_text(encoding="utf-8")

    def test_an_existing_link_is_left_alone_rather_than_written_again(self) -> None:
        """Idempotence here is the absence of a call, so the call log is what the test reads."""
        result, calls = self.apply([PROJECT_ID])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already linked to project 'Fleet Engineering'", result.stdout)
        self.assertEqual(calls.strip(), "")

    def test_a_missing_link_is_written_once(self) -> None:
        result, calls = self.apply(["PVT_other"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Linked ptr727/Fixture to project 'Fleet Engineering'", result.stdout)
        self.assertEqual(len(calls.strip().splitlines()), 1)
        self.assertIn("linkProjectV2ToRepository", calls)
        self.assertIn(PROJECT_ID, calls)
        self.assertIn(REPO_ID, calls)

    def test_a_response_naming_another_repository_stops_the_run(self) -> None:
        """A write that came back describing something else is unconfirmed, which is not success."""
        result, _ = self.apply([], mutation_repo_id="R_kgDOsomethingelse")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unconfirmed", result.stderr)
        self.assertNotIn("Linked ptr727/Fixture", result.stdout)


class WiringCase(unittest.TestCase):
    """The two call sites that decide whether any of the above runs at all.

    Each harness lifts the calling function whole and stubs the groups beside the one under
    test, because a diff that deletes a call leaves every helper passing its own tests while
    the group it belongs to never runs.
    """

    def test_apply_preflights_the_project_payload_before_any_write(self) -> None:
        """A payload outside the contract has to abort before the first of the four earlier writes."""
        with tempfile.TemporaryDirectory() as tmp:
            files = {}
            for name in ("settings", "labels", "develop", "main", "project"):
                path = Path(tmp) / f"{name}.json"
                path.write_text("{}", encoding="utf-8")
                files[name] = shlex.quote(str(path))
            preflight = lift(
                r"(    # Pre-flight every required payload before any write.*?\n    fi\n)    echo"
            )
            script = (
                f"settings_file={files['settings']}\nlabels_file={files['labels']}\n"
                f"develop_ruleset={files['develop']}\nmain_ruleset={files['main']}\n"
                f"project_file={files['project']}\n"
                "labels_payload_ok() { return 0; }\nproject_payload_ok() { return 1; }\n"
                f"cmd_apply() {{\n    local f\n{preflight}    echo reached-the-writes\n}}\ncmd_apply\n"
            )
            result = run_bash(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Aborting before any write", result.stderr)
        self.assertNotIn("reached-the-writes", result.stdout)

    def test_apply_stops_when_the_project_payload_is_not_there(self) -> None:
        """The existence loop is what a partial carry hits, and it runs before the contract test."""
        with tempfile.TemporaryDirectory() as tmp:
            files = {}
            for name in ("settings", "labels", "develop", "main"):
                path = Path(tmp) / f"{name}.json"
                path.write_text("{}", encoding="utf-8")
                files[name] = shlex.quote(str(path))
            absent = shlex.quote(str(Path(tmp) / "project.json"))
            preflight = lift(
                r"(    # Pre-flight every required payload before any write.*?\n    fi\n)    echo"
            )
            script = (
                f"settings_file={files['settings']}\nlabels_file={files['labels']}\n"
                f"develop_ruleset={files['develop']}\nmain_ruleset={files['main']}\n"
                f"project_file={absent}\n"
                "labels_payload_ok() { return 0; }\nproject_payload_ok() { return 0; }\n"
                f"cmd_apply() {{\n    local f\n{preflight}    echo reached-the-writes\n}}\ncmd_apply\n"
            )
            result = run_bash(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project.json not found", result.stderr)
        self.assertNotIn("reached-the-writes", result.stdout)

    def test_check_runs_the_project_group(self) -> None:
        """The group is only reached because cmd_check names it, and nothing else pins that."""
        body = lift(r"^(cmd_check\(\) \{\n.*?\n\}\n)")
        stubs = "".join(
            f"{name}() {{ :; }}\n"
            for name in (
                "check_ruleset",
                "check_settings",
                "check_security",
                "check_labels",
                "check_environments",
            )
        )
        script = (
            f"repo=ptr727/Fixture\nmodel=release\ndevelop_ruleset=d\nmain_ruleset=m\nFAILED=0\n"
            f'note() {{ :; }}\n{stubs}check_project() {{ echo "the project group ran"; }}\n'
            f"{body}cmd_check\n"
        )
        result = run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the project group ran", result.stdout)


class CheckGroupCase(unittest.TestCase):
    """The check half's own group, whose two reads are stubbed because each is tested above."""

    def harness(
        self, linked: list[str], node_id_status: int = 0
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "project.json"
            payload.write_text(
                json.dumps({"owner": "ptr727", "number": 1, "title": "Fleet Engineering"}),
                encoding="utf-8",
            )
            live = json.dumps({"id": REPO_ID, "projects": linked})
            script = (
                f"repo=ptr727/Fixture\nproject_file={shlex.quote(str(payload))}\n{JQR}"
                f"{lift(REPORTERS)}\n{lift(JQ_HAS)}{PAYLOAD_OK}{lift(ASSERT)}"
                f'project_node_id() {{ printf "%s\\n" {PROJECT_ID}; return {node_id_status}; }}\n'
                f"repo_projects() {{ printf '%s' {json.dumps(live)}; }}\n"
                f'{lift(CHECK_PROJECT)}check_project\necho "FAILED=$FAILED"\n'
            )
            return run_bash(script, "jq", "sed")

    def test_the_declared_link_passes(self) -> None:
        result = self.harness([PROJECT_ID, "PVT_other"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok   linked to project 'Fleet Engineering'", result.stdout)
        self.assertIn("FAILED=0", result.stdout)

    def test_a_missing_link_fails_the_run(self) -> None:
        result = self.harness(["PVT_other"])
        self.assertIn("FAIL linked to project 'Fleet Engineering'", result.stdout)
        self.assertIn("FAILED=1", result.stdout)

    def test_an_unreadable_project_fails_without_ending_the_run(self) -> None:
        """A read that failed is a failed group rather than an abort that skips the groups after it."""
        result = self.harness([PROJECT_ID], node_id_status=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not resolve the declared project", result.stdout)
        self.assertIn("FAILED=1", result.stdout)

    def test_other_links_are_counted_and_not_asserted(self) -> None:
        result = self.harness([PROJECT_ID, "PVT_other", "PVT_third"])
        self.assertIn("projects linked beyond the declared one: 2", result.stdout)
        self.assertIn("FAILED=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
