#!/usr/bin/env python3
"""Protect release and audit boundaries from fail-open regressions."""

from __future__ import annotations

import json
import os
import re
import shutil
import unittest
from pathlib import Path
from subprocess import run

REPO = Path(__file__).resolve().parents[2]


def hash_files(pattern: str, present: set[str]) -> bool:
    """Whether a workflow `hashFiles(<pattern>)` would match anything in `present`.

    `**` spans directory separators and `*` does not, which is what separates a root-only
    `requirements*.txt` from a recursive `tests/**`.
    """
    regex = re.escape(pattern).replace(r"\*\*", "@@").replace(r"\*", "[^/]*").replace("@@", ".*")
    return any(re.fullmatch(regex, path) for path in present)


def split_top_level(expression: str, operator: str) -> list[str]:
    """Split on `operator` outside any parentheses."""
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(expression):
        character = expression[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif depth == 0 and expression.startswith(operator, index):
            parts.append(expression[start:index])
            index += len(operator)
            start = index
            continue
        index += 1
    parts.append(expression[start:])
    return [part.strip() for part in parts]


def evaluate_guard(expression: str, present: set[str]) -> bool:
    """Evaluate a workflow `if:` written only from `hashFiles(...)` emptiness tests, `&&`, `||`, `()`.

    Deliberately narrow rather than a general expression engine: it is here to answer what the
    validator's Python leg does for one file set, not to reimplement GitHub's evaluator.
    """

    def atom(text: str) -> bool:
        match = re.fullmatch(r"hashFiles\('([^']*)'\)\s*(!=|==)\s*''", text.strip())
        if not match:
            raise ValueError(f"unsupported guard atom: {text!r}")
        hit = hash_files(match.group(1), present)
        return hit if match.group(2) == "!=" else not hit

    result = True
    for clause in split_top_level(expression, "&&"):
        if clause.startswith("(") and clause.endswith(")"):
            result = result and any(
                atom(alternative) for alternative in split_top_level(clause[1:-1], "||")
            )
        else:
            result = result and atom(clause)
    return result


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

        # The prefix loop above passes on the action's own directory name, so it fails open alone.
        # A producer typo leaves the publish job downloading nothing, and a loose delete filter blanket-deletes.
        producer = (REPO / ".github/actions/pypi-build-default/action.yml").read_text(
            encoding="utf-8"
        )
        consumer = (REPO / "docs/reusable-workflows.md").read_text(encoding="utf-8")
        self.assertRegex(producer, r"(?m)^[ \t]*name: pypi-build-\$\{\{ inputs\.branch \}\}[ \t]*$")
        self.assertRegex(
            consumer, r"(?m)^[ \t]*name: pypi-build-\$\{\{ github\.ref_name \}\}[ \t]*$"
        )
        self.assertIn('select(.name == \\"pypi-build-${{ github.ref_name }}\\")', consumer)

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

        # The prefix loop above passes on the action's own directory name, so it fails open alone.
        # A producer typo leaves the publish job downloading nothing, and a loose delete filter blanket-deletes.
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
        self.assertIn('select(.name == \\"nuget-build-${{ github.ref_name }}\\")', consumer)

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

    def test_validator_python_leg_reaches_a_pip_dependency_repo(self) -> None:
        """WORKFLOW.md D1.6 owes coverage to every Python repo with tests, uv-managed or not.

        Gating the leg on `uv.lock` alone skipped a pip/requirements repo that has tests, so it
        collected no coverage and never reached the missing-report failure either.
        """
        workflow = (REPO / ".github/workflows/validate-task.yml").read_text(encoding="utf-8")
        job = workflow.split("\n  unit-test:\n", 1)[1].split("\n  validate:\n", 1)[0]
        guards = [
            " ".join(line.strip() for line in block.strip().splitlines())
            for block in re.findall(r"(?m)^        if: >-\n((?:^ {10}.*\n)+)", job)
        ]
        python_guards = [guard for guard in guards if "tests/**" in guard]

        # Setup, dependency install, pytest, and upload: one drifting guard reintroduces the skip.
        self.assertEqual(4, len(python_guards))
        self.assertEqual(1, len(set(python_guards)))

        trees = {
            "uv project with tests": ({"pyproject.toml", "uv.lock", "tests/test_a.py"}, True),
            "pip project with tests": (
                {"pyproject.toml", "requirements.txt", "requirements-test.txt", "tests/test_a.py"},
                True,
            ),
            "tests but no dependency manifest": ({"pyproject.toml", "tests/test_a.py"}, False),
            "lint-only scripts tree": ({"pyproject.toml", "scripts/tool.py"}, False),
            "pip project with no tests": ({"pyproject.toml", "requirements.txt"}, False),
        }
        for label, (present, expected) in trees.items():
            with self.subTest(tree=label):
                self.assertEqual(expected, evaluate_guard(python_guards[0], present))

        # The guard admitting a pip repo is only half of it: the steps must install and run without a lockfile.
        self.assertIn('requirement_args+=(-r "$file")', job)
        self.assertIn('uv pip install "${requirement_args[@]}"', job)
        self.assertIn(".venv/bin/python -m pytest --cov-report=xml", job)

        # One resolve over every requirements file, never one install per file.
        # The glob sorts the base file last, so a per-file install lets its pins downgrade what the test-requirements file just resolved.
        self.assertNotIn('uv pip install -r "$file"', job)

        # The lockfile branch installs the project itself, so the pip branch owes the same.
        # Without it a src-layout repo fails collection on its own package instead of running its tests.
        self.assertIn("uv pip install -e .", job)
        self.assertIn(r"grep -Eq '^[[:space:]]*\[project\]' pyproject.toml", job)

    def test_validator_pytest_leg_fans_out_over_every_named_interpreter(self) -> None:
        """A pinned interpreter drops an adopter's other legs with nothing failing or warning.

        The required check goes green on the one version the task ran while the repository goes on
        claiming the rest, so the fan-out is asserted here rather than left to be read by eye.
        """
        workflow = (REPO / ".github/workflows/validate-task.yml").read_text(encoding="utf-8")
        job = workflow.split("\n  unit-test:\n", 1)[1].split("\n  validate:\n", 1)[0]

        # The default's shape is asserted and its content is not.
        # Neither bumping the fleet default nor adding a second interpreter to it is a regression here.
        declaration = workflow.split("      python-versions:\n", 1)[1].split("\n    secrets:", 1)[0]
        default = re.search(r"(?m)^        default: ['\"](.*)['\"]$", declaration)
        self.assertIsNotNone(default)
        assert default is not None
        versions = json.loads(default.group(1))
        self.assertTrue(versions)
        self.assertTrue(all(isinstance(version, str) for version in versions))

        # An explicit name: is used verbatim rather than falling back to a matrix-suffixed default.
        # Without the interpolation every leg renders one indistinguishable check name.
        self.assertIn("    name: Unit test job (Python ${{ matrix.python-version }})\n", job)

        # The matrix reads the input and the uv setup reads the matrix, so no literal survives between them.
        self.assertIn("        python-version: ${{ fromJSON(inputs.python-versions) }}\n", job)
        self.assertIn("          python-version: ${{ matrix.python-version }}\n", job)
        self.assertNotIn('python-version: "', job)

        # One interpreter failing must not cancel the others, which is what a second leg is run to learn.
        self.assertIn("      fail-fast: false\n", job)

        # Without a flag naming its leg, each upload merges into one number that hides which leg it came from.
        self.assertIn("          flags: python-${{ matrix.python-version }}\n", job)

    @unittest.skipUnless(shutil.which("jq"), "jq is what the step under test runs")
    def test_validator_refuses_a_python_versions_value_fromjson_would_admit(self) -> None:
        """fromJSON admits a JSON array of numbers, which is not a list of interpreter versions.

        An empty entry is the value that would otherwise carry a run green on an interpreter nobody
        chose, since setup-uv reads an empty input as an absent one. An unquoted entry announces
        itself either way. The step's whole script is run here, not just its filter, since
        inverting the condition or exiting zero would leave a filter-only assertion green while
        the step admitted everything.
        """
        workflow = (REPO / ".github/workflows/validate-task.yml").read_text(encoding="utf-8")
        job = workflow.split("\n  unit-test:\n", 1)[1].split("\n  validate:\n", 1)[0]

        # The guard has to precede the steps it guards, so its position is asserted, not just its presence.
        # Matched on the dash rather than on a name: key, or a step leading with uses: would slip in ahead unseen.
        first_step = re.search(r"(?m)^      - (.*)$", job)
        self.assertIsNotNone(first_step)
        assert first_step is not None
        self.assertEqual("name: Validate python-versions input step", first_step.group(1))

        marker = "      - name: Validate python-versions input step\n"
        self.assertIn(marker, job)
        body = job.split(marker, 1)[1]
        opener = re.search(r"(?m)^        run: \|-?\n", body)
        self.assertIsNotNone(opener, "the guard's script must be a literal block scalar")
        assert opener is not None
        script = body[opener.end() :]
        lines: list[str] = []
        for line in script.splitlines():
            if line and not line.startswith(" " * 10):
                break
            lines.append(line[10:])
        script = "\n".join(lines)
        self.assertIn("jq -e", script)

        cases = {
            # Reachable: a non-empty JSON array expands into legs whatever its entries hold.
            '["3.13"]': 0,
            '["3.13", "3.14"]': 0,
            # Reached by interpolating an unset value, and green on an unpinned interpreter if admitted.
            '[""]': 1,
            # The same path, since setup-uv trims before it decides an input is absent.
            '[" "]': 1,
            # The plausible slip, the input being a quoted string already, and well-formed JSON.
            "[3.13, 3.14]": 1,
            # Unreachable today: each of these fails while the matrix is expanded, before the step runs.
            # They pin the rest of the filter's contract, which moving the check into a job of its own would ask for.
            "[]": 1,
            '"3.13"': 1,
            "{}": 1,
            "3.13": 1,
            "": 1,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                verdict = run(
                    ["bash", "-c", script],
                    env={**os.environ, "PYTHON_VERSIONS": value},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(expected, verdict.returncode)

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
