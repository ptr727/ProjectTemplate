#!/usr/bin/env python3
"""Protect release and audit boundaries from fail-open regressions."""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from subprocess import run
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[2]


SHELL_LABELS = ("bash", "sh", "shell")


class FencedBlock(NamedTuple):
    """One fenced code block: the 1-based line its opener sits on, its label, and its body."""

    line: int
    label: str
    body: str


# A run of three or more backticks or tildes opens or closes a code block.
# This scan reads one only where nothing precedes the run but up to three spaces.
# What follows the run carries no backtick or tilde of its own.
FENCE_RUN = re.compile(r"`{3,}|~{3,}")
FENCE_LINE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^`~]*)$")


def fenced_blocks(markdown: str) -> list[FencedBlock]:
    """Every fenced code block in `markdown`, dedented by its own opener's indent.

    The label is filtered by the caller rather than by the scan, because a scanner that skips
    an unwanted opener walks into its body, so a block illustrating Markdown would hand out the
    blocks it contains.

    A line is refused unless the fence run is the whole of it bar up to three leading spaces
    and an info string carrying no backtick or tilde. Reading one
    needs the container it sits in, since a renderer measures a fence's indent against its list
    item's content column and takes four spaces past that as an indented code block instead, and
    a scan that guesses at the container reads a block a renderer does not or misses one it
    does. Refusing instead makes a shape this cannot read fail loudly rather than go unchecked,
    which is the whole reason the scan exists, and widening it is a deliberate edit here.

    Being a plain fence line is necessary and not sufficient, so some legal Markdown is refused
    too, a fence under a second list level and a shorter fence inside a longer one among it.
    No document here uses any of it.

    One container case is left rather than refused. An HTML block holding an odd number of
    fence-shaped lines flips the open and closed parity, since a renderer reads the block as raw
    HTML and this scan reads each of those lines as a fence, and an unlabelled block can be lost
    that way. A labelled opener landing in closer position always raises on its info string, so
    a block carrying a label, which is the only kind this is read for, cannot be lost through
    it. Recognising an HTML block needs the container reasoning above, so the bound is what
    holds rather than the shape being handled.

    Only a space or a tab may follow a closing fence, and only a space carries a body's indent.
    Python calls a character whitespace, or a line ending, where a renderer keeps it as content.
    `str.strip` with no argument, `str.isspace`, `str.splitlines` and a newline-only split each
    read one of those as whitespace to discard. They closed a block early and left the rest
    unread, ate the start of a body line, rewrote a separator as a line ending, and swallowed a
    body opened on a carriage return. Naming too wide a class does the same: a blank test
    of spaces and tabs took a tab for an indent and sliced a column a renderer expands.
    Every comparison here names the
    characters it means, and a test of its own holds that rule over this function's source,
    since writing the next comparison bare is how each of those arrived.
    """
    blocks: list[FencedBlock] = []
    # A renderer ends a line on a newline, and on a carriage return alone or paired.
    # It ends one on nothing else.
    # `str.splitlines` also breaks on separators a renderer keeps as text.
    # The rejoin below would then hand back a line ending the document never carried.
    # Splitting on the newline alone drops the other way, reading a lone return as text.
    # That loses the body of a block opened on one.
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    opener: re.Match[str] | None = None
    opened_at = 0
    body: list[str] = []
    for number, line in enumerate(lines, start=1):
        fence = FENCE_LINE.match(line)
        if FENCE_RUN.search(line) and not fence:
            raise AssertionError(f"line {number} carries a fence run that is not a fence line")
        if not fence:
            if opener is not None:
                body.append(line)
            continue
        if opener is None:
            opener, opened_at, body = fence, number, []
            continue
        open_fence = opener.group("fence")
        close = fence.group("fence")
        if (
            close[0] != open_fence[0]
            or len(close) < len(open_fence)
            or fence.group("info").strip(" \t")
        ):
            raise AssertionError(
                f"line {number} does not close the block opened at line {opened_at}"
            )
        indent = len(opener.group("indent"))
        # A renderer strips up to the opener's indent from each body line, expanding a tab first.
        # Inside a container a line carrying less than that ends it, and so ends the block.
        # Telling those apart needs the container this scan refuses to reason about.
        # A body line not simply carrying the indent in spaces is refused rather than guessed at.
        # A blank line here is one of spaces alone.
        # A tab is not an indent, since a renderer expands it to a column a slice cannot find.
        for offset, line in enumerate(body):
            if line.strip(" ") and line[:indent] != " " * indent:
                raise AssertionError(
                    f"line {opened_at + 1 + offset} is indented less than the block's opener"
                )
        # A renderer takes the label as the info string's first word.
        # Python strips wider whitespace than a renderer does, either side of the token.
        # Where a renderer's first word is a label, only spaces and tabs precede it, which
        # Python strips too, so this can read a label a renderer does not and never miss one.
        # An over-read adds a block to the parse, where a missed label would drop one from it.
        words = opener.group("info").split()  # any-whitespace: over-reads only, see above
        blocks.append(
            FencedBlock(
                opened_at,
                words[0] if words else "",
                "\n".join(line[indent:] for line in body),
            )
        )
        opener = None
    if opener is not None:
        raise AssertionError(f"unterminated block opened at line {opened_at}")
    return blocks


# A call whose argument does not open with a quote is taken as unnamed, which over-reaches.
# A named constant passed by name reads the same here as no argument at all.
# The conservative direction is the right one.
# Its cost is a comment, where the alternative is the class of defect this exists to stop.
BARE_PREDICATE = re.compile(r"\.(strip|lstrip|rstrip|split|rsplit|splitlines|isspace)\((?![\"'])")
# A wide class written into a pattern does the same as a bare call.
# `re.UNICODE` is the default for a `str` pattern.
# Only the `\\s` spelling is reached, not `\\S` or a set built from either
# (ptr727/ProjectTemplate#1618).
WIDE_CLASS = re.compile(r"\\s")


def bare_whitespace_predicates(source: str) -> tuple[list[str], int]:
    """Unmarked lines of `source` taking Python's whitespace class, and the count of marked ones.

    The count is of predicates rather than lines, since two on one line are two decisions.
    """
    offenders: list[str] = []
    exempt = 0
    for line in source.split("\n"):
        hits = len(BARE_PREDICATE.findall(line)) + len(WIDE_CLASS.findall(line))
        if not hits:
            continue
        if "any-whitespace:" in line:
            exempt += hits
            continue
        offenders.append(line.strip())  # any-whitespace: a report of a line, not a parse of one
    return offenders, exempt


BASH_ONLY = ("<(", "<<<", "$'", "[[")


def mislabeled_posix_blocks(markdown: str) -> list[tuple[int, str]]:
    """Blocks labelled as POSIX shell whose body needs bash, as (line, label) pairs."""
    return [
        (block.line, block.label)
        for block in fenced_blocks(markdown)
        if block.label.lower() in {"sh", "shell"}
        and any(token in block.body for token in BASH_ONLY)
    ]


def shell_blocks(markdown: str) -> list[str]:
    """The body of every fenced block in `markdown` whose label names a shell."""
    return [b.body for b in fenced_blocks(markdown) if b.label.lower() in SHELL_LABELS]


def fenced_shell_block(markdown: str, marker: str) -> str:
    """The one fenced shell block in `markdown` containing `marker`.

    Anchoring on what a block is about rather than on a variable name inside it keeps this
    finding the block after the block is rewritten, which is how a rename silently stopped the
    probe below from being exercised at all.
    """
    blocks = [block for block in shell_blocks(markdown) if marker in block]
    if len(blocks) != 1:
        raise AssertionError(f"expected one shell block mentioning {marker!r}, found {len(blocks)}")
    return blocks[0]


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
        self.assertIn('select(.name == "pypi-build-" + env.GITHUB_REF_NAME)', consumer)

        tracked_text = run(
            ["git", "grep", "-n", legacy_name],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual("", tracked_text.stdout)
        self.assertEqual(1, tracked_text.returncode)

    def test_pypi_default_versions_from_the_semver2_core(self) -> None:
        """The hub PyPI default stamps SemVer2's M.N.P core rather than AssemblyFileVersion.

        For a two-part version.json base NBGV fills AssemblyFileVersion's fourth segment from the
        commit id, so a version built from it never equals the release tag. The four-part refusal
        case below is that shape.
        """
        action = (REPO / ".github/actions/pypi-build-default/action.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("assembly-file-version", action)

        marker = "    - name: Compute PyPI version step\n"
        self.assertIn(marker, action)
        body = action.split(marker, 1)[1]
        opener = re.search(r"(?m)^      run: \|-?\n", body)
        self.assertIsNotNone(opener, "the version step's script must be a literal block scalar")
        assert opener is not None
        lines: list[str] = []
        for line in body[opener.end() :].splitlines():
            if line and not line.startswith(" " * 8):
                break
            lines.append(line[8:])
        script = "\n".join(lines)

        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch) / "output"

            def compute(branch: str, semver2: str) -> int:
                output.write_text("", encoding="utf-8")
                env = {**os.environ, "BRANCH": branch, "SEMVER2": semver2}
                env["GITHUB_OUTPUT"] = str(output)
                verdict = run(
                    ["bash", "-c", script],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                return verdict.returncode

            stamped = {
                ("main", "1.2.34"): "1.2.34",
                ("develop", "1.2.34-g1a2b3c4d5e"): "1.2.34.dev0",
                ("main", "1.2.34+build.7"): "1.2.34",
            }
            for (branch, semver2), expected in stamped.items():
                with self.subTest(branch=branch, semver2=semver2):
                    self.assertEqual(0, compute(branch, semver2))
                    self.assertEqual(f"version={expected}\n", output.read_text(encoding="utf-8"))

            # An unset input would otherwise stamp an empty version, and four parts is the shape the defect stamped.
            for semver2 in ("", "1.2", "1.2.34.51234"):
                with self.subTest(semver2=semver2):
                    self.assertEqual(1, compute("main", semver2))
                    self.assertEqual("", output.read_text(encoding="utf-8"))

            # The rejected value is printed for diagnosis, and neither command syntax may survive in it.
            # The runner reads a :: command only at a line start, but a ##[ command anywhere in a line.
            for injected in ("1.2\n::error::injected", "1.2\n##[error]injected"):
                with self.subTest(injected=injected):
                    output.write_text("", encoding="utf-8")
                    env = {**os.environ, "BRANCH": "main", "SEMVER2": injected}
                    env["GITHUB_OUTPUT"] = str(output)
                    verdict = run(
                        ["bash", "-c", script],
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        check=False,
                    )
                    self.assertEqual(1, verdict.returncode)
                    self.assertIn("injected", verdict.stdout)
                    lines = verdict.stdout.splitlines()
                    self.assertEqual(1, len([line for line in lines if line.startswith("::")]))
                    self.assertNotIn("##[", verdict.stdout)

            # The version step compares the branch but never prints it.
            output.write_text("", encoding="utf-8")
            injected = "x\n##[error]injected\n::error::injected"
            env = {**os.environ, "BRANCH": injected, "SEMVER2": "1.2.34"}
            env["GITHUB_OUTPUT"] = str(output)
            verdict = run(
                ["bash", "-c", script],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, verdict.returncode)
            self.assertEqual("version=1.2.34\n", output.read_text(encoding="utf-8"))
            self.assertNotIn("injected", verdict.stdout + verdict.stderr)

        # Both hook steps receive SemVer2, the caller hook as the dotnet-publish and build-nuget hooks already do.
        workflow = (REPO / ".github/workflows/build-release-task.yml").read_text(encoding="utf-8")
        job = workflow.split("\n  build-pypi:\n", 1)[1].split("\n  build-docker:\n", 1)[0]
        self.assertEqual(
            2, job.count("          semver2: ${{ needs.get-version.outputs.SemVer2 }}\n")
        )

    def test_release_gate_refuses_a_branch_git_would_not_name(self) -> None:
        """validate-release refuses a branch git would not accept as a name, on a smoke run too.

        Such a value can carry a newline and a :: command, or a ##[ token the runner reads anywhere
        in a line, into any later step that prints the branch. The refusal never echoes it.
        """
        workflow = (REPO / ".github/workflows/build-release-task.yml").read_text(encoding="utf-8")
        marker = "      - name: Validate branch and version consistency step\n"
        self.assertIn(marker, workflow)
        body = workflow.split(marker, 1)[1]
        opener = re.search(r"(?m)^        run: \|-?\n", body)
        self.assertIsNotNone(opener, "the gate's script must be a literal block scalar")
        assert opener is not None
        lines: list[str] = []
        for line in body[opener.end() :].splitlines():
            if line and not line.startswith(" " * 10):
                break
            lines.append(line[10:])
        script = "\n".join(lines)

        def gate(branch: str, semver2: str, smoke: str = "false") -> tuple[int, str]:
            env = {**os.environ, "BRANCH": branch, "SEMVER2": semver2, "SMOKE": smoke}
            verdict = run(
                ["bash", "-c", script],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            return verdict.returncode, verdict.stdout + verdict.stderr

        self.assertEqual(0, gate("develop", "1.2.34-g1a2b3c4d5e")[0])
        self.assertEqual(0, gate("feature/x-1", "1.2.34", smoke="true")[0])

        # A prerelease version, so only the branch check can refuse it.
        injected = "x\n##[error]injected\n::error::injected"
        for smoke in ("false", "true"):
            with self.subTest(smoke=smoke):
                code, output = gate(injected, "1.2.34-g1a2b3c4d5e", smoke=smoke)
                self.assertEqual(1, code)
                commands = [line for line in output.splitlines() if line.startswith("::")]
                self.assertEqual(1, len(commands))
                self.assertNotIn("##[", output)
                self.assertNotIn("injected", output)

        # A valid branch reaches the version check, whose refusal names the version it rejected.
        code, output = gate("feature/x-1", "1.2.34")
        self.assertEqual(1, code)
        self.assertIn("'1.2.34'", output)

    def test_no_run_script_interpolates_a_ref_name(self) -> None:
        """No run: script interpolates an expression that `names_a_ref` flags.

        An expression is pasted into the script before bash parses it, and git accepts a branch
        named like x$(id), so an interpolated name would run as code in a job holding the token.
        """
        paths = [
            *sorted((REPO / ".github/workflows").glob("*.y*ml")),
            *sorted((REPO / ".github/actions").glob("*/action.y*ml")),
            *sorted((REPO / "catalog/snippets/workflows").glob("*.y*ml")),
            REPO / "docs/reusable-workflows.md",
        ]
        expression = re.compile(r"\$\{\{((?:(?!\}\}).)*)(?:\}\}|$)")
        bracket = re.compile(r"\[\s*(?:'([\w-]+)'|\d+|\*)\s*\]")
        ref_name = re.compile(
            r"\b(?:inputs\.branch|github\.(?:ref_name|head_ref|base_ref|ref)"
            r"|github\.event\.[\w.*]*(?:ref|branch))\b"
        )

        def names_a_ref(line: str) -> bool:
            return any(
                ref_name.search(bracket.sub(r".\1", body)) for body in expression.findall(line)
            )

        opener = re.compile(r"^( *)(- )?run:")
        blocks = 0
        offenders: list[str] = []
        for path in paths:
            block_indent: int | None = None
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                indent = len(line) - len(line.lstrip(" "))
                if block_indent is not None and line.strip() and indent <= block_indent:
                    block_indent = None
                match = opener.match(line)
                if match:
                    blocks += 1
                    block_indent = len(match.group(1)) + len(match.group(2) or "")
                if block_indent is not None and names_a_ref(line):
                    offenders.append(f"{path.relative_to(REPO)}:{number}")
        # A scanner that recognized no run: block would pass on any tree.
        self.assertGreater(blocks, 50)
        self.assertEqual([], offenders)
        for form in (
            "${{ github.event.pull_request.head.ref }}",
            "${{ github.event.workflow_run.head_branch }}",
            "${{ format('{0}', github.ref_name) }}",
            "${{ inputs['branch'] }}",
            "${{ github[ 'ref_name' ] }}",
            "${{ github.event['pull_request'].head['ref'] }}",
            "${{ github.event.workflow_run.pull_requests[0].head.ref }}",
            "${{ github.event.workflow_run.pull_requests.*.head.ref }}",
        ):
            with self.subTest(form=form):
                self.assertTrue(names_a_ref(form))
        for form in (
            "${{ github.event_name }} ${{ github.run_id }}",
            "${{ github['event_name'] }}",
        ):
            with self.subTest(form=form):
                self.assertFalse(names_a_ref(form))

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
        self.assertIn('select(.name == "nuget-build-" + env.GITHUB_REF_NAME)', consumer)

        tracked_text = run(
            ["git", "grep", "-n", legacy_name],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
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

    def test_no_bare_whitespace_predicate_in_the_block_scanner(self) -> None:
        r"""The block scanner names the characters it calls whitespace, everywhere.

        Python calls a character whitespace, or a line ending, where a renderer keeps it as
        content. Five silent defects in this scanner were one bare predicate each, and each was
        written by someone fixing the previous one, so the rule is worth more than the fixes.
        A comparison needing a wider class than the characters it names states that inline and
        says why, and the exemptions are counted so a second cannot be added quietly.

        A sixth defect named its characters and named one too many, `strip(" \t")` where only a
        space carries an indent, and this cannot see that: any argument opening with a quote is
        taken as deliberate. Naming a class is where the rule stops and reading the class against
        the format begins, which is what the behaviour cases are for.

        The four functions named below are read, rather than only the one that had the defects,
        since moving a predicate into another of them is the cheapest way past a check that reads
        one. Module-level code is not read, and the set of functions is a literal here rather than
        derived, so a predicate placed outside both is caught by the behaviour cases or not at all
        (ptr727/ProjectTemplate#1618).
        """
        # The detector is run against source written to fail it.
        # One that only ever sees a clean file can stop working with nothing to show for it.
        dirty = [
            "    return line.strip()",
            "    return line.split(None)",
            "    return line.split(maxsplit=1)",
            "    return line.rsplit()",
            "    return line.isspace()",
            "    return line.lstrip()",
            "    return line.rstrip()",
            "    return line.splitlines()",
            "    return str.strip(line)",
            '    return re.sub(r"\\s", "", line)',
        ]
        for line in dirty:
            with self.subTest(dirty=line.strip()):
                self.assertEqual(([line.strip()], 0), bare_whitespace_predicates(line))
        self.assertEqual(([], 0), bare_whitespace_predicates('    return line.strip(" \t")'))
        self.assertEqual(
            ([], 1), bare_whitespace_predicates("    x = y.split()  # any-whitespace:")
        )
        # The marker's colon is load-bearing, since the word alone appears in ordinary prose.
        self.assertEqual(
            (["x = y.split()  # any-whitespace"], 0),
            bare_whitespace_predicates("    x = y.split()  # any-whitespace"),
        )
        # Exemptions are counted per predicate, so two on one line cannot pass as one.
        self.assertEqual(
            ([], 2),
            bare_whitespace_predicates("    x = y.split() + z.strip()  # any-whitespace:"),
        )

        offenders: list[tuple[str, str]] = []
        exempt = 0
        for function in (fenced_blocks, shell_blocks, mislabeled_posix_blocks, fenced_shell_block):
            found, marked = bare_whitespace_predicates(inspect.getsource(function))
            offenders.extend((function.__name__, line) for line in found)
            exempt += marked
        self.assertEqual([], offenders, "a whitespace predicate here names its characters")
        # The escape hatch is the narrow kind, so it is counted rather than trusted.
        self.assertEqual(1, exempt, "each reasoned exception is reviewed on its own")

    def test_shell_block_extractor_reads_every_fence_shape(self) -> None:
        """A block the extractor skips is a block the guard silently never tests.

        Each case pins one rule, and a rule no case exercises can be dropped without any test
        noticing, so a rule added to the extractor is owed a case here. The read cases are the
        shapes this scan accepts rather than the ones these documents happen to use, since a
        rule is worth pinning before a document reaches for it. Everything else is refused,
        because reading it would need the container the fence sits in, and a wrong guess there
        is the silent miss this scan exists to prevent.
        """
        read = [
            ("three backticks", "```bash\nA=1\n```\n", ["A=1"]),
            ("a longer fence", "````bash\nB=2\n````\n", ["B=2"]),
            ("a closer longer than its opener", "```bash\nB=3\n````\n", ["B=3"]),
            ("a tilde fence", "~~~sh\nC=3\n~~~\n", ["C=3"]),
            ("a longer tilde fence", "~~~~sh\nC=4\n~~~~\n", ["C=4"]),
            # Two backticks are a code span's delimiter, not a fence, so this opens nothing.
            ("a two-backtick run", "``bash\nC=5\n``\n", []),
            # Whitespace after a closing fence is not text after it, so the block still closes.
            ("trailing space on the closer", "```bash\nC=8\n``` \n", ["C=8"]),
            ("a tab after the closer", "```bash\nC=9\n```\t\n", ["C=9"]),
            # The label is the first word of the info string, wherever the whitespace falls.
            ("space before the label", "``` bash\nC=11\n```\n", ["C=11"]),
            # A carriage return ends a line for a renderer, alone as well as paired.
            ("a lone carriage return", "```bash\rC=12\r```\r", ["C=12"]),
            ("carriage returns paired", "```bash\r\nC=13\r\n```\r\n", ["C=13"]),
            # The opener's indent is what the body is dedented by, not the closer's.
            ("a closer less indented than its opener", "- x\n\n  ```bash\n  C=10\n```\n", ["C=10"]),
            # A separator a renderer treats as text must not come back as a line ending.
            ("a line separator inside a body", "```bash\nC=6\u2028C=7\n```\n", ["C=6\u2028C=7"]),
            ("indented inside a list item", "- x\n\n  ```shell\n  D=4\n  ```\n", ["D=4"]),
            ("a closer three spaces in", "```bash\nD=5\n   ```\n", ["D=5"]),
            ("an info string with attributes", '```bash title="x"\nH=11\n```\n', ["H=11"]),
            ("a mixed-case label", "```Bash\nI=12\n```\n", ["I=12"]),
            ("an empty block", "```bash\n```\n", [""]),
            # A blank body line carries no indent to check and keeps what sits past the opener's.
            ("a whitespace-only body line", "  ```bash\n  A\n    \n  B\n  ```\n", ["A\n  \nB"]),
            # A blank line shorter than the opener's indent is still blank, and keeps nothing.
            ("a blank line shorter than the indent", "  ```bash\n  A\n \n  B\n  ```\n", ["A\n\nB"]),
            ("an empty body line", "  ```bash\n  A\n\n  B\n  ```\n", ["A\n\nB"]),
            ("a label that is not a shell", "```python\nK=15\n```\n", []),
            ("a label merely starting with one", "```shell-session\n$ x\n```\n", []),
            ("an info string of only spaces", "```   \nK=18\n```\n", []),
            ("an inline code span", "Run `bash` now.\n", []),
            # A run of mixed characters is not a fence run at all, so a body may carry one.
            ("a body mixing both fence characters", "```bash\nC=14=`~`\n```\n", ["C=14=`~`"]),
            ("two blocks back to back", "```bash\nL=1\n```\n```sh\nL=2\n```\n", ["L=1", "L=2"]),
        ]
        for name, markdown, expected in read:
            with self.subTest(read=name):
                self.assertEqual(expected, shell_blocks(markdown))

        refused = [
            # A fence sharing a line with anything else needs the container to read.
            # Guessing at the container is what silently drops or swallows a block.
            ("on a list-marker line", "- ```bash\nM=1\n  ```\n"),
            ("in a blockquote", "> ```bash\nM=2\n> ```\n"),
            ("in a nested blockquote", "> > ```bash\nM=3\n> > ```\n"),
            ("behind a tab", "\t```bash\nM=4\n\t```\n"),
            ("four spaces in", "Para.\n\n    ```bash\n    M=5\n    ```\n"),
            ("a fence run mid-line", "text ```bash text\nM=6\n```\n"),
            # A body quoting a fence is the refusal an author here is likeliest to reach.
            # A scan taking it for content would read the rest of the document as body.
            (
                "a fence run inside an open block",
                "```bash\nprintf '%s' '```bash'\nM=6b\n```\n",
            ),
            # A fence run is one character repeated.
            # A mixed run opens nothing.
            # A scan reading it as one would close it too, taking in a block with no fence in it.
            ("a mixed fence run", "```~~~\nM=6c\n```~~~\n"),
            ("a backtick in the info string", "```bash `x`\nM=7\n```\n"),
            ("a tilde in a backtick fence's info", "```bash ~x\nM=7b\n```\n"),
            ("a tilde fence in a blockquote", "> ~~~bash\n> M=7c\n> ~~~\n"),
            # Only a space or a tab may follow a closing fence.
            # A character a renderer treats as content leaves the rest of a real block unread.
            # The document closes cleanly.
            # A scan accepting the no-break space returns a short block rather than raising.
            # This case then fails rather than passing by luck.
            (
                "a no-break space after the closer",
                "```bash\nM=7d\n```\u00a0\nrm -rf /\n```\u00a0\nM=7e\n```\n",
            ),
            # A closer must match its opener's character and length and carry nothing after it.
            # Otherwise the block it ends is not the block that was opened.
            ("a shorter fence closing a longer one", "````bash\nM=8\n```\nM=9\n````\n"),
            ("a tilde closing a backtick fence", "```bash\nM=10\n~~~\n"),
            ("trailing text on the closer", "```bash\nM=11\n``` no\n"),
            ("a fence indented past the bound inside a body", "```bash\nM=12\n    ```\n"),
            ("an unterminated block", "padding\n\n```bash\nM=13\n"),
            # A renderer strips up to the opener's indent.
            # It ends the container on a line carrying less than that.
            # Such a line is not this block's body as printed.
            ("a body line indented partway", "- x\n\n  ```bash\n  M=14\n M=15\n  ```\n"),
            ("a body line at column zero", "- x\n\n  ```bash\n  M=16\nM=17\n  ```\n"),
            ("a tab where the indent should be", "  ```bash\n  M=18\n\tM=19\n  ```\n"),
            # A tab is not an indent even on an otherwise blank line.
            # A renderer expands it to a column, where this scan slices characters.
            ("a body line of one tab", "  ```bash\n  M=25\n\t\n  M=26\n  ```\n"),
            # Only a space carries an indent.
            # A merely blank-looking character is not one, so the line is not the body as printed.
            ("a body line opening on a no-break space", "  ```bash\n\u00a0\u00a0M=20\n  ```\n"),
            # A line of characters Python calls blank is content to a renderer.
            # A renderer ends the container on it, so the body as printed stops there.
            ("a body line of one no-break space", "  ```bash\n  M=21\n\u00a0\n  M=22\n  ```\n"),
            ("a body line of one form feed", "  ```bash\n  M=23\n\u000c\n  M=24\n  ```\n"),
        ]
        for name, markdown in refused:
            with self.subTest(refused=name), self.assertRaises(AssertionError):
                shell_blocks(markdown)

        # A closer shorter than its opener is refused as a bad close.
        # Left to surface as an unterminated block, it would name a line carrying no fence.
        with self.assertRaises(AssertionError) as short_close:
            shell_blocks("````bash\nM=8\n```\nM=9\n````\n")
        self.assertIn("does not close", str(short_close.exception))
        # The refusal names the offending line rather than wherever the scan stopped.
        with self.assertRaises(AssertionError) as unterminated:
            shell_blocks("padding\n\n```bash\nN=1\nN=2\n")
        self.assertIn("line 3", str(unterminated.exception))
        with self.assertRaises(AssertionError) as under_indented:
            shell_blocks("- x\n\n  ```bash\n  N=5\n N=6\n  ```\n")
        self.assertIn("line 5", str(under_indented.exception))
        with self.assertRaises(AssertionError) as midline:
            shell_blocks("ok\nok\n- ```bash\n")
        self.assertIn("line 3", str(midline.exception))
        # The line a block reports is its opener's, which only a failure would otherwise render.
        self.assertEqual(
            [(1, "bash"), (5, "sh")],
            [(b.line, b.label) for b in fenced_blocks("```bash\nN=3\n```\n\n```sh\nN=4\n```\n")],
        )
        # The single-block helper resolves exactly one block.
        # A rename leaving none and an edit leaving two each stop it testing what it names.
        two = "```bash\nmarker A\n```\n\n```sh\nmarker B\n```\n"
        self.assertEqual("marker A", fenced_shell_block("```bash\nmarker A\n```\n", "marker"))
        with self.assertRaises(AssertionError):
            fenced_shell_block(two, "marker")
        with self.assertRaises(AssertionError):
            fenced_shell_block("```bash\nnothing\n```\n", "marker")

    def test_audit_runnable_blocks_parse_as_printed(self) -> None:
        """Every bash block AUDIT.md tells the reader to run parses with its placeholders intact.

        No repository gate reads a snippet inside a Markdown file, and blocks here once shipped
        an unquoted `repo=<owner>/<repo>` and an unquoted `--repo <path-to-target-checkout>`,
        each of which bash parses as a redirection, so they died of a syntax error before any
        `set` header took effect. Checking the substituted form is what hid it, so this checks
        the printed form, and every shell label rather than `bash` alone, since the second of
        those sat in a `shell`-labeled block.
        """
        audit = (REPO / "AUDIT.md").read_text(encoding="utf-8")
        blocks = shell_blocks(audit)
        self.assertTrue(blocks, "AUDIT.md carries no shell block")
        for source in blocks:
            # An empty block has no first line to name.
            # Naming the subTest must not be what fails when the document is what is wrong.
            with self.subTest(block=(source.splitlines() or [""])[0][:60]):
                parsed = run(
                    ["bash", "-n", "-c", source],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(0, parsed.returncode, parsed.stderr)

    def test_audit_probes_fail_before_local_path_checks(self) -> None:
        """AUDIT.md's ecosystem probe reports an API failure rather than reading it as a clean tree.

        A failed read that returns nothing looks exactly like a repository declaring no
        ecosystem, so the probe has to exit non-zero rather than print a MISSING line nobody
        can tell from a real finding.
        """
        audit = (REPO / "AUDIT.md").read_text(encoding="utf-8")
        probe = fenced_shell_block(audit, "package-ecosystem").replace(
            'repo="<owner>/<repo>"', "repo=owner/name"
        )
        fake_api = r"""
gh() {
  # The workflows leg answers the count its --jq filter would compute, since this stub runs no jq.
  case "$2" in
    repos/*/contents/.github/dependabot.yml\?*) printf '%s\n' '- package-ecosystem: github-actions' '- package-ecosystem: devcontainers' ;;
    repos/*/contents/.github/workflows\?*) printf '1\n' ;;
    repos/*/contents/.github\?*) printf '%s\n' .github/dependabot.yml .github/workflows ;;
    repos/*/contents\?*) printf '%s\n' .devcontainer .github ;;
    *) return 17 ;;
  esac
}
"""

        success = run(
            ["bash", "-c", f"{fake_api}\n{probe}"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        failure = run(["bash", "-c", f"gh() {{ return 17; }}\n{probe}"], check=False)
        # One read failing is the fail-open the "every read fails" case above cannot see.
        one_read_fails = run(
            [
                "bash",
                "-c",
                f"{fake_api.replace('''repos/*/contents/.github\\?*) printf''', 'repos/*/contents/.github\\?*) return 17 ;; repos/*/never\\?*) printf')}\n{probe}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        # A declared name carrying a digit must not truncate to a shorter real name.
        # A commented-out entry must not count either.
        # This fixture carries one of each, so both ecosystems have to report as missing.
        missing_api = fake_api.replace(
            """printf '%s\\n' '- package-ecosystem: github-actions' '- package-ecosystem: devcontainers'""",
            """printf '%s\\n' '# - package-ecosystem: devcontainers' '  - package-ecosystem: \"github-actions2\"'""",
        )
        missing = run(
            ["bash", "-c", f"{missing_api}\n{probe}"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(0, success.returncode, success.stderr)
        self.assertIn("github-actions: present", success.stdout)
        self.assertIn("devcontainers: present", success.stdout)
        # Every read failing must stop the run, never reach the per-ecosystem lines.
        self.assertNotEqual(0, failure.returncode)
        # So must any single read failing, rather than reporting an absence it never established.
        self.assertNotEqual(0, one_read_fails.returncode, one_read_fails.stdout)
        # Both MISSING arms have to be reachable, or the block could stop reporting and still pass.
        self.assertEqual(0, missing.returncode, missing.stderr)
        self.assertIn("github-actions: MISSING", missing.stdout)
        self.assertIn("devcontainers: MISSING", missing.stdout)
        # The suppressed-output probe this guard replaced must not come back.
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
        chose, since setup-uv reads an empty input as an absent one. An unquoted entry is admitted
        by fromJSON as a number, and uv resolves some of those rather than refusing them, so that
        shape carries a run green too. The step's whole script is run here, not just its filter, since
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
            # The integer form of it, which uv resolves rather than refuses.
            "[3]": 1,
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
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual(expected, verdict.returncode)

        # The annotation carries the rejected value back.
        # The runner reads a workflow command off any line starting with one, so an unescaped newline starts a second.
        injected = '["3.13"]\n::error::injected'
        verdict = run(
            ["bash", "-c", script],
            env={**os.environ, "PYTHON_VERSIONS": injected},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(1, verdict.returncode)
        emitted = (verdict.stdout + verdict.stderr).splitlines()
        self.assertEqual(1, len(emitted))
        self.assertIn("%0A", emitted[0])

        # A carriage return is the third escape, and without this nothing here would notice its removal.
        verdict = run(
            ["bash", "-c", script],
            env={**os.environ, "PYTHON_VERSIONS": "x\ry"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(1, verdict.returncode)
        self.assertIn("got: x%0Dy", verdict.stdout + verdict.stderr)

        # A bare percent must reach the annotation encoded.
        # Their order is what the %0A assertion above covers instead, not this one.
        # Substituting the percent last renders a newline %250A, which fails there and passes here.
        verdict = run(
            ["bash", "-c", script],
            env={**os.environ, "PYTHON_VERSIONS": "%"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(1, verdict.returncode)
        self.assertIn("got: %25", verdict.stdout + verdict.stderr)

    def test_audit_bash_blocks_are_not_labeled_as_posix_shell(self) -> None:
        audit = (REPO / "AUDIT.md").read_text(encoding="utf-8")

        self.assertEqual([], mislabeled_posix_blocks(audit))
        # The same predicate is exercised on documents that must trip it.
        # One that only ever sees a clean file can stop working with nothing to show for it.
        # Every token and every spelling of the label is covered.
        # Losing one of either is a check that quietly stops catching a whole shape.
        bodies = {
            "<(": "diff <(a) b",
            "<<<": 'grep -q x <<<"$y"',
            "$'": "x=$'a\\n'",
            "[[": "[[ -n $x ]]",
        }
        for label in ("sh", "shell", "Shell"):
            for token, body in bodies.items():
                with self.subTest(label=label, token=token):
                    self.assertEqual(
                        [(1, label)],
                        mislabeled_posix_blocks(f"```{label}\n{body}\n```\n"),
                    )


if __name__ == "__main__":
    unittest.main()
