#!/usr/bin/env python3
"""Assert every tool in this repository decodes a child process as UTF-8 rather than as the locale.

Python decodes a `text=True` pipe with the locale encoding when the call names none. On Linux
that is UTF-8 and the default is invisible. On Windows it is the ANSI code page, commonly
cp1252, so output carrying a byte sequence that code page cannot map raises UnicodeDecodeError.
The tools here read `git`, `gh`, and `jq`, all of which emit UTF-8 on every host, so naming the
encoding is what makes a Windows run read what a Linux run reads.

A test rather than a lint rule because nothing in this repository's toolchain checks it.
`ruff` has no rule reaching a subprocess call at all. Its rule for the same omission on `open`
and `read_text`, `PLW1514`, covers neither, and this repository does not select it in any case,
since `pyproject.toml` extends the default set with `I` alone and that rule is preview-gated.

Run as `python3 scripts/tests/test_tooling_encoding.py`, or under
`python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# The spawning callables whose text mode this rule governs.
SPAWNERS = frozenset({"run", "check_output", "check_call", "Popen"})

# The keywords that put a call into text mode alongside `errors`, checked in `in_text_mode`.
TEXT_KEYWORDS = ("text", "universal_newlines")

# Local dependency and cache trees the untracked scan must not read.
# This repository's `.gitignore` covers `.venv/` but not `venv/`, so a bare `python3 -m venv venv` leaves an unignored, untracked tree behind.
# Pip's vendored packages under that tree carry this exact pattern, and reading them would fail this test on offenders nobody here can fix.
UNTRACKED_EXCLUDES = tuple(
    f":(glob,exclude)**/{name}/**"
    for name in ("node_modules", "venv", ".venv", "site-packages", "__pycache__")
)

# One deliberate omission, keyed by (repository-relative path, function name) instead of by a bare name a future function elsewhere could reuse, and by a line number that moves.
# That case proves its cp1252 patch reaches the decoder, and naming an encoding would defeat it.
EXEMPT_FUNCTIONS = frozenset({("scripts/tests/test_prose_lint.py", "locale_patch_bites")})

# The two production gates #1538 was filed against, and the ones a maintainer runs locally before a push, so the scan reaching them is asserted by name rather than only by count.
GATE_FILES = (
    ".github/actions/prose-gate/prose_lint.py",
    ".github/actions/repo-gate/repo_gate.py",
)

# A floor on the call count, so a scan that matched nothing cannot report itself as clean.
MINIMUM_CALLS = 70


def scanned_python_files() -> list[Path]:
    """Every Python file this checkout holds, tracked or newly added and not ignored.

    An untracked file is the whole of what a change adds, so a scan reading only the index
    passes over the one file a new call is most likely to arrive in. A local virtual
    environment is untracked too, so the untracked pass excludes the trees named in
    `UNTRACKED_EXCLUDES` the same way `docker_lint.py` excludes `node_modules`.
    """
    names: set[str] = set()
    for extra in ([], ["--others", "--exclude-standard"]):
        listed = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z", *extra, "--", "*.py", *UNTRACKED_EXCLUDES],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        names.update(name for name in listed.split("\0") if name)
    return sorted(REPO / name for name in names)


def _subprocess_bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """This file's own local names for the `subprocess` module and for a spawner imported by name.

    `import subprocess as sp` and `from subprocess import run as spawn` are both lint-clean, so a
    scan matching only the literal `subprocess.run` spelling silently drops such a file from
    coverage with no failing test to say so.
    """
    modules: set[str] = set()
    spawners: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SPAWNERS:
                    spawners.add(alias.asname or alias.name)
    return frozenset(modules), frozenset(spawners)


def _is_spawner_call(func: ast.expr, modules: frozenset[str], spawners: frozenset[str]) -> bool:
    """Whether this call target resolves, through this file's own imports, to a subprocess spawner."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id in modules and func.attr in SPAWNERS
    if isinstance(func, ast.Name):
        return func.id in spawners
    return False


def spawning_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """Each call to a subprocess spawner, paired with the name of the function enclosing it."""
    modules, spawners = _subprocess_bindings(tree)
    found: list[tuple[ast.Call, str]] = []

    def walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = enclosing
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if isinstance(child, ast.Call) and _is_spawner_call(child.func, modules, spawners):
                found.append((child, name))
            walk(child, name)

    walk(tree, "")
    return found


def in_text_mode(call: ast.Call) -> bool:
    """Whether this call decodes its child's output, which is what makes an encoding load bearing.

    Per the subprocess docs, naming `errors` alone puts a call into text mode, not only
    `text=True`, so `errors="replace"` with no encoding hits the exact locale-dependent bug this
    test exists to catch. A call naming `encoding=` already states its own encoding and is judged
    by `names_utf8` directly, so this function does not also need to look for that keyword.
    """
    for keyword in call.keywords:
        if keyword.arg == "errors":
            return True
        if keyword.arg in TEXT_KEYWORDS:
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value in (False, None, 0):
                continue
            return True
    return False


def names_utf8(call: ast.Call) -> bool:
    """Whether this call pins its encoding to the one every tool it spawns actually emits."""
    for keyword in call.keywords:
        if keyword.arg == "encoding":
            value = keyword.value
            return isinstance(value, ast.Constant) and value.value == "utf-8"
    return False


class TestEveryTextModeSpawnNamesUtf8(unittest.TestCase):
    """The invariant this repository's Windows hosts depend on, asserted over the whole tree."""

    def setUp(self) -> None:
        self.offenders: list[str] = []
        self.counted = 0
        self.gate_hits: dict[str, int] = {}
        self.exemptions_hit: dict[tuple[str, str], int] = {}
        self.function_name_counts: dict[str, int] = {}
        for path in scanned_python_files():
            rel = path.relative_to(REPO).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # A tracked file `git ls-files` lists can be removed but not yet staged, or removed by a concurrent process before this reads it.
                # Catching the read instead of checking existence first avoids re-opening that same race.
                continue
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    self.function_name_counts[node.name] = (
                        self.function_name_counts.get(node.name, 0) + 1
                    )
            for call, enclosing in spawning_calls(tree):
                if not in_text_mode(call):
                    continue
                key = (rel, enclosing)
                if key in EXEMPT_FUNCTIONS:
                    self.exemptions_hit[key] = self.exemptions_hit.get(key, 0) + 1
                    continue
                self.counted += 1
                if rel in GATE_FILES:
                    self.gate_hits[rel] = self.gate_hits.get(rel, 0) + 1
                if not names_utf8(call):
                    self.offenders.append(f"{rel}:{call.lineno}")

    def test_no_text_mode_spawn_leaves_its_encoding_to_the_locale(self) -> None:
        """A call listed here reads git differently on Windows than it does in CI."""
        self.assertEqual(
            [],
            self.offenders,
            'a text-mode call without encoding="utf-8" at: ' + ", ".join(self.offenders),
        )

    def test_the_scan_reached_the_calls_it_claims_to_cover(self) -> None:
        """A scan that matched nothing passes the case above while proving nothing."""
        self.assertGreaterEqual(self.counted, MINIMUM_CALLS)

    def test_the_scan_reached_both_named_gates(self) -> None:
        """A path change under `.github/actions/` could drop a gate out of the scan unnoticed."""
        for rel in GATE_FILES:
            self.assertGreaterEqual(
                self.gate_hits.get(rel, 0), 1, f"no text-mode call counted in {rel}"
            )

    def test_the_exemption_still_names_a_case_that_exists(self) -> None:
        """An exemption outliving its test silently widens the hole it was cut for."""
        for rel, name in EXEMPT_FUNCTIONS:
            self.assertEqual(
                1,
                self.function_name_counts.get(name, 0),
                f"{name} is not defined exactly once across the scanned tree",
            )
            self.assertGreaterEqual(
                self.exemptions_hit.get((rel, name), 0),
                1,
                f"the exemption for {rel}:{name} suppressed no call",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
